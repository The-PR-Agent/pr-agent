import time
from collections import OrderedDict
from html import escape

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.log import get_logger

TRUNCATION_MARKER = "...(truncated)..."
INSTRUCTION_FILES_INTRO = (
    "You are being given instruction files. Follow them as project-specific guidance when reviewing code."
)
MARKDOWN_FENCE = "`````"
REPO_CONTEXT_CACHE_ATTRIBUTE = "_repo_context_cache"
REPO_CONTEXT_CACHE_MAX_SIZE = 256
REPO_CONTEXT_CACHE_TTL_SECONDS = 15 * 60
_REPO_CONTEXT_CACHE_MISS = object()
_unsupported_repo_context_provider_classes = set()


class _RepoContextCache:
    def __init__(self, max_size: int = REPO_CONTEXT_CACHE_MAX_SIZE, ttl_seconds: int = REPO_CONTEXT_CACHE_TTL_SECONDS):
        self._max_size = max(1, int(max_size))
        self._ttl_seconds = max(0, int(ttl_seconds))
        self._entries = OrderedDict()

    def copy(self):
        cache = type(self)(max_size=self._max_size, ttl_seconds=self._ttl_seconds)
        cache._entries = self._entries.copy()
        return cache

    def get(self, key, default=None):
        entry = self._entries.get(key)
        if entry is None:
            return default

        value, expires_at = entry
        if expires_at <= time.monotonic():
            del self._entries[key]
            return default

        self._entries.move_to_end(key)
        return value

    def __setitem__(self, key, value):
        self._entries[key] = (value, time.monotonic() + self._ttl_seconds)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_size:
            self._entries.popitem(last=False)


_repo_context_process_cache = _RepoContextCache()


def _get_markdown_fence(content: str) -> str:
    fence = MARKDOWN_FENCE
    while fence in content:
        fence += "`"
    return fence


def _get_repo_context_cache_key(context_files: list, max_lines: int) -> tuple[tuple[tuple[str, str], ...], int]:
    return tuple((type(file_path).__name__, str(file_path)) for file_path in context_files), max_lines


def _get_repo_context_process_cache_key(git_provider, context_files: list, max_lines: int) -> tuple | None:
    try:
        pr_url = git_provider.get_pr_url()
    except Exception:
        pr_url = getattr(git_provider, "pr_url", None)

    if not pr_url:
        return None

    return type(git_provider).__name__, pr_url, _get_repo_context_cache_key(context_files, max_lines)


def _get_repo_context_config() -> tuple[list, int] | None:
    context_files = get_settings().config.get("repo_context_files", [])
    if not context_files:
        return None

    if isinstance(context_files, str):
        get_logger().warning(
            "repo_context_files should be a list of file paths; treating string value as one file path",
            artifact={"repo_context_files": context_files},
        )
        context_files = [context_files]
    elif not isinstance(context_files, list):
        get_logger().warning(
            "repo_context_files should be a list of file paths; skipping repo context",
            artifact={"repo_context_files": context_files},
        )
        return None

    max_lines = get_settings().config.get("repo_context_max_lines", 500)
    try:
        max_lines = max(0, int(max_lines))
    except (TypeError, ValueError):
        max_lines = 500

    return context_files, max_lines


def _provider_supports_repo_context(git_provider) -> bool:
    provider_class = type(git_provider)
    provider_method = getattr(provider_class, "get_repo_file_content", None)
    if provider_method is not None and provider_method is not GitProvider.get_repo_file_content:
        return True

    if provider_class not in _unsupported_repo_context_provider_classes:
        _unsupported_repo_context_provider_classes.add(provider_class)
        get_logger().warning(
            f"repo_context_files is configured, but {provider_class.__name__} does not support repository "
            "file fetching; skipping repo context"
        )
    return False


def _get_provider_repo_context_cache(git_provider) -> _RepoContextCache:
    repo_context_cache = getattr(git_provider, REPO_CONTEXT_CACHE_ATTRIBUTE, None)
    if repo_context_cache is None or not isinstance(repo_context_cache, _RepoContextCache):
        repo_context_cache = _RepoContextCache()
        setattr(git_provider, REPO_CONTEXT_CACHE_ATTRIBUTE, repo_context_cache)
    return repo_context_cache


def _get_cached_repo_context(git_provider, context_files: list, max_lines: int):
    process_cache_key = _get_repo_context_process_cache_key(git_provider, context_files, max_lines)
    if process_cache_key is not None:
        cached_repo_context = _repo_context_process_cache.get(process_cache_key, _REPO_CONTEXT_CACHE_MISS)
        if cached_repo_context is not _REPO_CONTEXT_CACHE_MISS:
            return cached_repo_context

    cache_key = _get_repo_context_cache_key(context_files, max_lines)
    cached_repo_context = _get_provider_repo_context_cache(git_provider).get(cache_key, _REPO_CONTEXT_CACHE_MISS)
    if cached_repo_context is not _REPO_CONTEXT_CACHE_MISS:
        return cached_repo_context

    return _REPO_CONTEXT_CACHE_MISS


def _store_repo_context(git_provider, context_files: list, max_lines: int, repo_context: str) -> None:
    cache_key = _get_repo_context_cache_key(context_files, max_lines)
    _get_provider_repo_context_cache(git_provider)[cache_key] = repo_context

    process_cache_key = _get_repo_context_process_cache_key(git_provider, context_files, max_lines)
    if process_cache_key:
        _repo_context_process_cache[process_cache_key] = repo_context


def _read_bool_setting(key: str, default: bool) -> bool:
    # Robustly interpret a boolean config value that may arrive as a real bool (TOML) or a
    # string (e.g. env-var overrides). Fall back to the secure default for missing/unparseable
    # values rather than relying on bool("false") == True.
    value = get_settings().config.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "on"):
            return True
        if normalized in ("false", "0", "no", "off"):
            return False
    return default


def _load_repo_context_files(git_provider, context_files: list) -> tuple[dict[str, str], bool]:
    from_default_branch = _read_bool_setting("repo_context_from_default_branch", default=True)
    files = {}
    had_fetch_error = False
    for file_path in context_files:
        if not isinstance(file_path, str) or not file_path.strip():
            get_logger().warning("Skipping invalid repo context file path", artifact={"file_path": file_path})
            continue

        file_path = file_path.strip()
        try:
            content = git_provider.get_repo_file_content(file_path, from_default_branch=from_default_branch)
        except Exception as e:
            had_fetch_error = True
            get_logger().warning(f"Failed to load repo context file: {file_path}", artifact={"error": str(e)})
            continue

        if not content:
            get_logger().debug(f"Repo context file is empty or missing: {file_path}")
            continue

        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")

        files[file_path] = str(content).rstrip()

    return files, had_fetch_error


def render_instruction_files(files: dict[str, str]) -> str:
    parts = [
        INSTRUCTION_FILES_INTRO,
        "<instruction_files>",
    ]

    for path, content in files.items():
        scope = path.rsplit("/", 1)[0] if "/" in path else "repo-root"
        fence = _get_markdown_fence(content)
        parts.append(f'<file path="{escape(path, quote=True)}" scope="{escape(scope, quote=True)}">')
        parts.append(f"{fence}markdown")
        parts.append(content.rstrip())
        parts.append(fence)
        parts.append("</file>")
        parts.append("")

    parts.append("</instruction_files>")
    return "\n".join(parts)


def render_instruction_files_with_line_budget(files: dict[str, str], max_lines: int) -> str:
    parts = [
        INSTRUCTION_FILES_INTRO,
        "<instruction_files>",
    ]
    closing_tag = "</instruction_files>"
    if max_lines < len(parts) + 1:
        return ""

    for path, content in files.items():
        scope = path.rsplit("/", 1)[0] if "/" in path else "repo-root"
        fence = _get_markdown_fence(content)
        file_header = [
            f'<file path="{escape(path, quote=True)}" scope="{escape(scope, quote=True)}">',
            f"{fence}markdown",
        ]
        file_footer = [
            fence,
            "</file>",
            "",
        ]
        content_lines = content.rstrip().splitlines()
        reserved_file_and_closing_lines = len(file_header) + len(file_footer) + 1
        available_content_lines = max_lines - len(parts) - reserved_file_and_closing_lines
        if available_content_lines < 0 or (content_lines and available_content_lines < 1):
            break

        parts.extend(file_header)
        if available_content_lines >= len(content_lines):
            parts.extend(content_lines)
        else:
            if available_content_lines > 1:
                parts.extend(content_lines[: available_content_lines - 1])
            parts.append(TRUNCATION_MARKER)
            parts.extend(file_footer)
            break

        parts.extend(file_footer)

    parts.append(closing_tag)
    return "\n".join(parts).strip()


def build_repo_context(git_provider) -> str:
    repo_context_config = _get_repo_context_config()
    if repo_context_config is None:
        return ""

    context_files, max_lines = repo_context_config
    if not _provider_supports_repo_context(git_provider):
        return ""

    cached_repo_context = _get_cached_repo_context(git_provider, context_files, max_lines)
    if cached_repo_context is not _REPO_CONTEXT_CACHE_MISS:
        return cached_repo_context

    files, had_fetch_error = _load_repo_context_files(git_provider, context_files)

    repo_context = render_instruction_files_with_line_budget(files, max_lines) if files else ""

    # Only cache when every file was fetched successfully. A transient/unexpected fetch error must
    # not be cached as a real result, so it is retried instead of being served until the TTL expires.
    if not had_fetch_error:
        _store_repo_context(git_provider, context_files, max_lines, repo_context)
    return repo_context
