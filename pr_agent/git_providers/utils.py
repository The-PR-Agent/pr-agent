import copy
import os
import posixpath
import tempfile
import traceback
import urllib.parse

from dynaconf import Dynaconf
from starlette_context import context

from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.log import get_logger

# Baseline extra_instructions values captured before the first metadata injection.
# Used in non-context runtimes (CLI, polling) to restore clean state between PRs,
# preventing metadata from one PR leaking into the next.
_extra_instructions_baseline: dict[str, str] = {}

def _is_safe_repo_file_path(file_path: str) -> bool:
    """
    Validate that a file path is safe to read from a repository root.
    Rejects absolute paths, paths with '..' traversal components, backslashes,
    and percent-encoded bypass attempts.
    """
    if not file_path or not file_path.strip():
        return False
    # Decode percent-encoded sequences (e.g. %2e%2e/) before validation to prevent bypass
    file_path = urllib.parse.unquote(file_path)
    # Reject paths that still contain '%' after decoding (double-encoding, ambiguous encodings)
    if "%" in file_path:
        return False
    # Reject absolute paths (Unix and Windows-style)
    if os.path.isabs(file_path) or file_path.startswith("/") or file_path.startswith("\\"):
        return False
    if len(file_path) >= 2 and file_path[1] == ":":  # e.g. C:\...
        return False
    # Reject backslashes (non-standard on most git providers, potential traversal vector)
    if "\\" in file_path:
        return False
    # Reject any ".." path segment to prevent directory traversal
    segments = file_path.replace("\\", "/").split("/")
    if ".." in segments:
        return False
    # Normalize and reject any ".." components as a defense-in-depth check
    normalized = posixpath.normpath(file_path)
    if normalized.startswith("..") or "/.." in normalized:
        return False
    return True


def apply_repo_settings(pr_url):
    os.environ["AUTO_CAST_FOR_DYNACONF"] = "false"
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

    # Repository metadata: fetch well-known instruction files (AGENTS.md, QODO.md, CLAUDE.md, …)
    # from the PR's head branch root and inject their contents into every tool's extra_instructions.
    # See: https://qodo-merge-docs.qodo.ai/usage-guide/additional_configurations/#bringing-additional-repository-metadata-to-pr-agent
    #
    # Guard: apply_repo_settings() can be called multiple times per request (e.g. once in the
    # server handler and again inside PRAgent.handle_request). The TOML settings are idempotent
    # (set/overwrite), but metadata is *appended* to extra_instructions, so we must skip on
    # repeated calls to avoid duplicating content in prompts.
    # In server mode we use the Starlette request context for a per-request flag. In CLI/polling
    # mode (no context), we restore extra_instructions to the pre-metadata baseline before each
    # application, preventing metadata from one PR leaking into the next.
    repo_metadata_applied = False
    try:
        repo_metadata_applied = context.get("repo_metadata_applied", False)
    except Exception:
        pass
    if not repo_metadata_applied and get_settings().config.get("add_repo_metadata", False):
        try:
            tool_sections = [
                "pr_reviewer",
                "pr_description",
                "pr_code_suggestions",
                "pr_add_docs",
                "pr_update_changelog",
                "pr_test",
                "pr_improve_component",
            ]

            # In non-context runtimes (CLI, polling), restore extra_instructions to their
            # pre-metadata baseline so metadata from a previous PR doesn't persist.
            global _extra_instructions_baseline
            is_context_mode = False
            try:
                is_context_mode = context.exists()
            except Exception:
                pass
            if not is_context_mode:
                if _extra_instructions_baseline:
                    # Restore baseline before applying this PR's metadata
                    for section, baseline_value in _extra_instructions_baseline.items():
                        get_settings().set(f"{section}.extra_instructions", baseline_value)
                else:
                    # First run: capture the current values as the baseline
                    for section in tool_sections:
                        section_obj = get_settings().get(section, None)
                        if section_obj is not None and hasattr(section_obj, "extra_instructions"):
                            _extra_instructions_baseline[section] = section_obj.extra_instructions or ""

            metadata_files = get_settings().config.get("add_repo_metadata_file_list",
                                                        ["AGENTS.md", "QODO.md", "CLAUDE.md"])

            # Collect contents of all metadata files that exist in the repo
            metadata_content_parts = []
            for file_name in metadata_files:
                if not _is_safe_repo_file_path(file_name):
                    get_logger().warning(f"Skipping unsafe metadata file path: '{file_name}'")
                    continue
                content = git_provider.get_repo_file(file_name)
                if content and content.strip():
                    metadata_content_parts.append(content.strip())
                    get_logger().info(f"Loaded repository metadata file: {file_name}")

            # Append combined metadata to extra_instructions for every tool that supports it.
            if metadata_content_parts:
                combined_metadata = "\n\n".join(metadata_content_parts)
                for section in tool_sections:
                    section_obj = get_settings().get(section, None)
                    if section_obj is not None and hasattr(section_obj, "extra_instructions"):
                        existing = section_obj.extra_instructions or ""
                        if existing:
                            new_value = f"{existing}\n\n{combined_metadata}"
                        else:
                            new_value = combined_metadata
                        get_settings().set(f"{section}.extra_instructions", new_value)
            # Mark as applied for this request (server mode only)
            try:
                context["repo_metadata_applied"] = True
            except Exception:
                pass
        except Exception as e:
            get_logger().debug(f"Failed to load repository metadata files: {e}")

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
