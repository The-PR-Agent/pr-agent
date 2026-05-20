import copy
import os
import tempfile
import traceback
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from dynaconf import Dynaconf
from starlette_context import context

from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.log import get_logger

_MAX_EXTRA_CONFIG_BYTES = 1 * 1024 * 1024  # 1 MB cap for a remote .toml
_FETCH_TIMEOUT_SECONDS = 10


def _resolve_extra_config_to_file(source: str):
    """
    Resolve --extra_config_url to a local readable .toml file.

    Accepts:
      - http:// or https:// URL: fetched via urllib (with optional auth header
        from PR_AGENT_EXTRA_CONFIG_AUTH_HEADER, e.g. "PRIVATE-TOKEN: xxx").
      - file:// URL: treated as a local path.
      - bare local path: used directly.

    Returns (path, is_temp). Caller must remove path if is_temp is True.
    Returns (None, False) if source can't be resolved.
    """
    if not source:
        return None, False

    parsed = urlparse(source)
    scheme = (parsed.scheme or '').lower()

    # Local path (bare or file://)
    if scheme in ('', 'file'):
        local_path = parsed.path if scheme == 'file' else source
        if os.path.isfile(local_path):
            return local_path, False
        get_logger().warning(f"Extra config not found at local path: {local_path}")
        return None, False

    if scheme not in ('http', 'https'):
        get_logger().warning(f"Unsupported scheme for extra config: {scheme}")
        return None, False

    # Fetch over HTTP(S)
    headers = {'Accept': 'text/plain, application/toml, */*'}
    auth_header = os.environ.get('PR_AGENT_EXTRA_CONFIG_AUTH_HEADER')
    if auth_header and ':' in auth_header:
        name, value = auth_header.split(':', 1)
        headers[name.strip()] = value.strip()

    try:
        req = Request(source, headers=headers, method='GET')
        with urlopen(req, timeout=_FETCH_TIMEOUT_SECONDS) as resp:
            data = resp.read(_MAX_EXTRA_CONFIG_BYTES + 1)
        if len(data) > _MAX_EXTRA_CONFIG_BYTES:
            get_logger().warning(
                f"Extra config exceeds {_MAX_EXTRA_CONFIG_BYTES} bytes, skipping: {source}"
            )
            return None, False
        fd, tmp_path = tempfile.mkstemp(suffix='.toml')
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        get_logger().info(f"Fetched extra config from {source} ({len(data)} bytes)")
        return tmp_path, True
    except Exception as e:
        get_logger().warning(f"Failed to fetch extra config from {source}: {e}")
        return None, False


def _apply_settings_from_file(path: str, label: str):
    """
    Merge an external .toml settings file into the global settings, section-by-section.
    Uses the same custom_merge_loader as repo-local settings so security checks
    (forbidden includes/preloads/loaders) apply consistently.
    """
    if not path or not os.path.isfile(path):
        return
    try:
        dynconf_kwargs = {
            'core_loaders': [],
            'loaders': ['pr_agent.custom_merge_loader'],
            'merge_enabled': True,
        }
        new_settings = Dynaconf(
            settings_files=[path],
            load_dotenv=False,
            envvar_prefix=False,
            **dynconf_kwargs,
        )
        for section, contents in new_settings.as_dict().items():
            if not contents:
                continue
            section_dict = copy.deepcopy(get_settings().as_dict().get(section, {}))
            for key, value in contents.items():
                section_dict[key] = value
            get_settings().unset(section)
            get_settings().set(section, section_dict, merge=False)
        get_logger().info(f"Applied {label} settings from {path}:\n{new_settings.as_dict()}")
    except Exception as e:
        get_logger().warning(f"Failed to apply {label} settings from {path}: {e}")


def apply_repo_settings(pr_url):
    os.environ["AUTO_CAST_FOR_DYNACONF"] = "false"
    git_provider = get_git_provider_with_context(pr_url)

    # Apply external/shared config first so repo-local .pr_agent.toml overrides it.
    extra_source = get_settings().get("CONFIG.EXTRA_CONFIG_URL", None)
    if extra_source:
        extra_path, extra_is_temp = _resolve_extra_config_to_file(extra_source)
        if extra_path:
            try:
                _apply_settings_from_file(extra_path, label="extra")
            finally:
                if extra_is_temp:
                    try:
                        os.remove(extra_path)
                    except Exception as e:
                        get_logger().error(
                            f"Failed to remove temp extra config {extra_path}: {e}"
                        )

    if get_settings().config.use_repo_settings_file:
        repo_settings_file = None
        try:
            try:
                repo_settings = context.get("repo_settings", None)
            except Exception:
                repo_settings = None
                pass
            if repo_settings is None:  # None is different from "", which is a valid value
                repo_settings = git_provider.get_repo_settings()
                try:
                    context["repo_settings"] = repo_settings
                except Exception:
                    pass

            error_local = None
            if repo_settings:
                repo_settings_file = None
                category = 'local'
                try:
                    fd, repo_settings_file = tempfile.mkstemp(suffix='.toml')
                    os.write(fd, repo_settings)

                    try:
                        dynconf_kwargs = {'core_loaders': [],  # DISABLE default loaders, otherwise will load toml files more than once.
                             'loaders': ['pr_agent.custom_merge_loader'],
                             # Use a custom loader to merge sections, but overwrite their overlapping values. Don't involve ENV variables.
                             'merge_enabled': True  # Merge multiple files; ensures [XYZ] sections only overwrite overlapping keys, not whole sections.
                         }

                        new_settings = Dynaconf(settings_files=[repo_settings_file],
                                                # Disable all dynamic loading features
                                                load_dotenv=False,  # Don't load .env files
                                                envvar_prefix=False,  # Drop DYNACONF for env. variables
                                                **dynconf_kwargs
                                                )
                    except TypeError as e:
                        # Fallback for older Dynaconf versions that don't support these parameters
                        get_logger().warning(
                            "Your Dynaconf version does not support disabled 'load_dotenv'/'merge_enabled' parameters. "
                            "Loading repo settings without these security features. "
                            "Please upgrade Dynaconf for better security.",
                            artifact={"error": e, "traceback": traceback.format_exc()})
                        new_settings = Dynaconf(settings_files=[repo_settings_file])

                    for section, contents in new_settings.as_dict().items():
                        if not contents:
                            # Skip excluded items, such as forbidden to load env.
                            get_logger().debug(f"Skipping a section: {section} which is not allowed")
                            continue
                        section_dict = copy.deepcopy(get_settings().as_dict().get(section, {}))
                        for key, value in contents.items():
                            section_dict[key] = value
                        get_settings().unset(section)
                        get_settings().set(section, section_dict, merge=False)
                    get_logger().info(f"Applying repo settings:\n{new_settings.as_dict()}")
                except Exception as e:
                    get_logger().warning(f"Failed to apply repo {category} settings, error: {str(e)}")
                    error_local = {'error': str(e), 'settings': repo_settings, 'category': category}

                if error_local:
                    handle_configurations_errors([error_local], git_provider)
        except Exception as e:
            get_logger().exception("Failed to apply repo settings", e)
        finally:
            if repo_settings_file:
                try:
                    os.remove(repo_settings_file)
                except Exception as e:
                    get_logger().error(f"Failed to remove temporary settings file {repo_settings_file}", e)

    # enable switching models with a short definition
    if get_settings().config.model.lower() == 'claude-3-5-sonnet':
        set_claude_model()


def handle_configurations_errors(config_errors, git_provider):
    try:
        if not any(config_errors):
            return

        for err in config_errors:
            if err:
                configuration_file_content = err['settings'].decode()
                err_message = err['error']
                config_type = err['category']
                header = f"❌ **PR-Agent failed to apply '{config_type}' repo settings**"
                body = f"{header}\n\nThe configuration file needs to be a valid [TOML](https://qodo-merge-docs.qodo.ai/usage-guide/configuration_options/), please fix it.\n\n"
                body += f"___\n\n**Error message:**\n`{err_message}`\n\n"
                if git_provider.is_supported("gfm_markdown"):
                    body += f"\n\n<details><summary>Configuration content:</summary>\n\n```toml\n{configuration_file_content}\n```\n\n</details>"
                else:
                    body += f"\n\n**Configuration content:**\n\n```toml\n{configuration_file_content}\n```\n\n"
                get_logger().warning(f"Sending a 'configuration error' comment to the PR", artifact={'body': body})
                # git_provider.publish_comment(body)
                if hasattr(git_provider, 'publish_persistent_comment'):
                    git_provider.publish_persistent_comment(body,
                                                            initial_header=header,
                                                            update_header=False,
                                                            final_update_message=False)
                else:
                    git_provider.publish_comment(body)
    except Exception as e:
        get_logger().exception(f"Failed to handle configurations errors", e)


def set_claude_model():
    """
    set the claude-sonnet-3.5 model easily (even by users), just by stating: --config.model='claude-3-5-sonnet'
    """
    model_claude = "bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0"
    get_settings().set('config.model', model_claude)
    get_settings().set('config.model_weak', model_claude)
    get_settings().set('config.fallback_models', [model_claude])
