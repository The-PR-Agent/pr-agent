import copy
import os
import re
import tempfile
import tomllib
import traceback
from urllib.parse import urlparse
from urllib.request import Request, url2pathname, urlopen

from dynaconf import Dynaconf
from dynaconf.loaders import env_loader
from starlette_context import context

from pr_agent.config_loader import get_settings
from pr_agent.custom_merge_loader import validate_file_security
from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.log import get_logger

# Sections that touch host-level capabilities and so cannot be fully configured
# from a repo's .pr_agent.toml. For each section listed here, only the keys in
# its allowlist may be overridden by repo settings; every other key is dropped
# with a warning.
#
# skills: `enabled` and `max_skills_tokens` are safe per-repo preferences (a repo
# can opt in to, or size, the host's admin-curated skill library). `paths` is NOT
# overridable: it points at the PR-Agent host's filesystem, so letting a repo set
# it would allow a malicious repo to read sensitive host files (e.g. ~/.ssh/*)
# into the LLM prompt. `paths` therefore stays host-only.
_REPO_OVERRIDABLE_KEYS_BY_HOST_SECTION = {
    "skills": frozenset({"enabled", "max_skills_tokens"}),
}

_MAX_EXTRA_CONFIG_BYTES = 1 * 1024 * 1024  # 1 MB cap for a remote .toml
_FETCH_TIMEOUT_SECONDS = 10
# Bare Windows drive-letter paths (e.g. "C:\\shared.toml", "D:/cfg.toml").
# urlparse() would otherwise interpret the drive letter as a URL scheme.
_WINDOWS_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _safe_url_for_log(url: str) -> str:
    """
    Render a URL safe for logging: strip userinfo (user:pass@) and the query
    string, both of which may carry credentials (e.g. ?private_token=...).
    Falls back to a redacted placeholder on any parse error.
    """
    try:
        parsed = urlparse(url)
        netloc = parsed.hostname or ''
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        return f"{parsed.scheme}://{netloc}{parsed.path}"
    except Exception:
        return "<extra config URL redacted>"


def _resolve_extra_config_to_file(source):
    """
    Resolve --extra_config_url to a local readable .toml file.

    Accepts:
      - http:// or https:// URL: fetched via urllib (with optional auth header
        from PR_AGENT_EXTRA_CONFIG_AUTH_HEADER, e.g. "PRIVATE-TOKEN: <token>").
      - file:// URL: treated as a local path.
      - bare local path: used directly.

    Returns (path, is_temp). Caller must remove path if is_temp is True.
    Returns (None, False) if source can't be resolved.

    Logs never include the raw URL — `_safe_url_for_log()` strips userinfo and
    query string so embedded credentials don't leak into CI logs.
    """
    # Validate / normalise the input at the boundary
    if not isinstance(source, str):
        get_logger().warning(
            f"Ignoring CONFIG.EXTRA_CONFIG_URL: expected str, got {type(source).__name__}"
        )
        return None, False
    source = source.strip()
    if not source:
        return None, False

    # Bare Windows drive-letter paths must be handled before urlparse() — it
    # would otherwise treat the drive letter as a URL scheme.
    if _WINDOWS_DRIVE_PATH_RE.match(source):
        if os.path.isfile(source):
            return source, False
        get_logger().warning(f"Extra config not found at local path: {source}")
        return None, False

    parsed = urlparse(source)
    scheme = (parsed.scheme or "").lower()

    # Local path (bare or file://)
    if scheme in ("", "file"):
        if scheme == "file":
            # Preserve any non-localhost netloc (UNC-style file://host/share/...)
            # and URL-decode percent-encoded path components via url2pathname.
            netloc = parsed.netloc or ""
            raw = parsed.path
            if netloc and netloc.lower() != "localhost":
                raw = f"//{netloc}{raw}"
            local_path = url2pathname(raw)
        else:
            local_path = source
        if os.path.isfile(local_path):
            return local_path, False
        get_logger().warning(f"Extra config not found at local path: {local_path}")
        return None, False

    if scheme not in ("http", "https"):
        get_logger().warning(f"Unsupported scheme for extra config: {scheme}")
        return None, False

    # Fetch over HTTP(S)
    safe_url = _safe_url_for_log(source)
    headers = {"Accept": "text/plain, application/toml, */*"}
    auth_header = os.environ.get("PR_AGENT_EXTRA_CONFIG_AUTH_HEADER")
    if auth_header:
        if ":" in auth_header:
            name, value = auth_header.split(":", 1)
            headers[name.strip()] = value.strip()
        else:
            # Surface misconfiguration instead of silently dropping the header.
            get_logger().warning(
                "PR_AGENT_EXTRA_CONFIG_AUTH_HEADER is set but malformed "
                "(expected '<HeaderName>: <value>'); ignoring."
            )

    try:
        req = Request(source, headers=headers, method="GET")
        with urlopen(req, timeout=_FETCH_TIMEOUT_SECONDS) as resp:
            data = resp.read(_MAX_EXTRA_CONFIG_BYTES + 1)
        if len(data) > _MAX_EXTRA_CONFIG_BYTES:
            get_logger().warning(
                f"Extra config exceeds {_MAX_EXTRA_CONFIG_BYTES} bytes, skipping: {safe_url}"
            )
            return None, False
        fd, tmp_path = tempfile.mkstemp(suffix=".toml")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        get_logger().info(f"Fetched extra config from {safe_url} ({len(data)} bytes)")
        return tmp_path, True
    except Exception as e:
        get_logger().warning(f"Failed to fetch extra config from {safe_url}: {e}")
        return None, False


def _reapply_env_overrides():
    """
    Re-run dynaconf's env_loader against the global settings so env-sourced
    values win over any keys just merged from a config file.

    Why: _apply_settings_from_file() and the repo-local merge below both
    overwrite section dicts wholesale. Without this re-application, an extra
    config file or repo .pr_agent.toml can silently replace a secret supplied
    via environment variable — breaking the documented precedence (env vars
    are the highest layer; see docs/usage-guide/configuration_options.md).
    """
    try:
        env_loader.load(get_settings())
    except Exception as e:
        # Never let a precedence-restoration error block apply_repo_settings;
        # log and continue with whatever state the merge left.
        get_logger().warning(f"Failed to re-apply env-var overrides: {e}")


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
            "core_loaders": [],
            "loaders": ["pr_agent.custom_merge_loader"],
            "merge_enabled": True,
        }
        try:
            new_settings = Dynaconf(
                settings_files=[path],
                load_dotenv=False,
                envvar_prefix=False,
                **dynconf_kwargs,
            )
        except TypeError as e:
            # Older Dynaconf versions don't accept load_dotenv / merge_enabled.
            # The fallback Dynaconf(...) call below skips our custom_merge_loader,
            # which is where validate_file_security() runs. Pre-validate the file
            # explicitly here so forbidden directives (includes, preloads, custom
            # loaders, ...) still cannot slip through on those older versions.
            try:
                with open(path, "rb") as f:
                    parsed_toml = tomllib.load(f)
                validate_file_security(parsed_toml, path)
            except Exception as sec_err:
                get_logger().warning(
                    f"Extra config failed security pre-validation; skipping: {sec_err}"
                )
                return

            get_logger().warning(
                "Your Dynaconf version does not support disabled "
                "'load_dotenv'/'merge_enabled' parameters. Loading extra config "
                "after explicit security pre-validation; some Dynaconf-level "
                "hardening flags are off. Please upgrade Dynaconf for better "
                "security.",
                artifact={"error": e, "traceback": traceback.format_exc()},
            )
            new_settings = Dynaconf(settings_files=[path])

        merged_sections = []
        for section, contents in new_settings.as_dict().items():
            if not contents:
                continue
            section_dict = copy.deepcopy(get_settings().as_dict().get(section, {}))
            for key, value in contents.items():
                section_dict[key] = value
            get_settings().unset(section)
            get_settings().set(section, section_dict, merge=False)
            merged_sections.append(section)
        # Restore env-var precedence: the section-level unset()/set() above can
        # silently overwrite values originally sourced from env vars. Replay
        # env_loader so the env layer remains the top of the precedence stack.
        _reapply_env_overrides()
        # Do NOT log the merged dict: external/repo .pr_agent.toml may contain
        # secrets (e.g. openai.key, gitlab.personal_access_token) that would
        # otherwise leak into CI logs. Section names are safe and sufficient
        # for debugging which file contributed what.
        get_logger().info(
            f"Applied {label} settings from {path} (sections merged: {sorted(merged_sections)})"
        )
    except Exception as e:
        get_logger().warning(f"Failed to apply {label} settings from {path}: {e}")


def apply_repo_settings(pr_url):
    os.environ["AUTO_CAST_FOR_DYNACONF"] = "false"

    # Apply external/shared config FIRST, before constructing the git provider:
    # provider initialisers (e.g. GitLabProvider reads GITLAB.PERSONAL_ACCESS_TOKEN
    # at __init__) need to see any provider-critical settings that come from the
    # extra file. Repo-local .pr_agent.toml is still applied later and overrides
    # the extra file on conflicting keys.
    extra_source = get_settings().get("CONFIG.EXTRA_CONFIG_URL", None)
    if isinstance(extra_source, str) and extra_source.strip():
        extra_path, extra_is_temp = _resolve_extra_config_to_file(extra_source)
        if extra_path:
            try:
                # _apply_settings_from_file() re-applies env-var overrides
                # itself, so env precedence is restored before the provider
                # is constructed below.
                _apply_settings_from_file(extra_path, label="extra")
            finally:
                if extra_is_temp:
                    try:
                        os.remove(extra_path)
                    except Exception as e:
                        get_logger().error(
                            f"Failed to remove temp extra config {extra_path}: {e}"
                        )
    elif extra_source is not None and not isinstance(extra_source, str):
        get_logger().warning(
            "Ignoring CONFIG.EXTRA_CONFIG_URL: expected str, got "
            f"{type(extra_source).__name__}"
        )

    git_provider = get_git_provider_with_context(pr_url)

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
                    try:
                        os.write(fd, repo_settings)
                    finally:
                        os.close(fd)

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
                        allowed_keys = _REPO_OVERRIDABLE_KEYS_BY_HOST_SECTION.get(section.lower())
                        if allowed_keys is not None:
                            rejected = [k for k in contents if k.lower() not in allowed_keys]
                            if rejected:
                                get_logger().warning(
                                    f"Ignoring host-only key(s) {rejected} in section [{section}] from repo "
                                    f"settings; only {sorted(allowed_keys)} may be set per-repo for this section"
                                )
                            contents = {k: v for k, v in contents.items() if k.lower() in allowed_keys}
                            if not contents:
                                continue
                        section_dict = copy.deepcopy(get_settings().as_dict().get(section, {}))
                        for key, value in contents.items():
                            section_dict[key] = value
                        get_settings().unset(section)
                        get_settings().set(section, section_dict, merge=False)
                    # Same precedence-restoration rationale as the extra-config
                    # path: env-sourced values must remain the highest layer.
                    _reapply_env_overrides()
                    # Do NOT log the merged dict: repo/global .pr_agent.toml may contain secrets
                    # (e.g. openai.key, gitlab.personal_access_token) that would otherwise leak into
                    # CI logs. Section names are safe and sufficient for debugging (same rationale as
                    # the extra-config path above).
                    get_logger().info(
                        f"Applying repo settings (sections: {sorted(new_settings.as_dict().keys())})"
                    )
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
                                                            final_update_message=False,
                                                            name="config-errors")
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
