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
_DEFAULT_MAX_SIBLING_CONTEXT_FILES = 5
# Absolute host-controlled ceiling on sibling-repo context fetches per build. The operator-facing
# repo_context_max_sibling_files setting is clamped to this value before any fetch begins, so a
# comment command or an unexpectedly large configured value cannot produce unbounded cross-repo
# API calls. It is intentionally not user-configurable.
_HARD_MAX_SIBLING_CONTEXT_FILES = 20
_SIBLING_REPO_SEPARATOR = ":"
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


def _get_repo_context_cache_key(
    context_files: list, max_lines: int, context_ref: str | None
) -> tuple[tuple[tuple[str, str], ...], int, str | None]:
    return (
        tuple((type(file_path).__name__, str(file_path)) for file_path in context_files),
        max_lines,
        context_ref,
    )


def _get_repo_context_process_cache_key(
    git_provider, context_files: list, max_lines: int, context_ref: str | None
) -> tuple | None:
    try:
        pr_url = git_provider.get_pr_url()
    except Exception:
        pr_url = getattr(git_provider, "pr_url", None)

    if not pr_url:
        return None

    return type(git_provider).__name__, pr_url, _get_repo_context_cache_key(
        context_files, max_lines, context_ref
    )


def _get_repo_context_config() -> tuple[list, int] | None:
    context_files = get_settings().config.get("repo_context_files", [])
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

    if not context_files:
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


def _provider_supports_sibling_repo_context(git_provider) -> bool:
    provider_method = getattr(type(git_provider), "get_sibling_repo_file_content", None)
    return provider_method is not None and provider_method is not GitProvider.get_sibling_repo_file_content


def _sibling_repo_context_entries(context_files: list) -> list[tuple[str, str]]:
    """Return the (repo_id, file_path) pairs that parse as sibling entries.

    Only entries that would really fetch from a sibling repository may force a cache bypass
    (build_repo_context), so this parses with the same rules the loader uses: malformed or
    non-sibling entries yield no pair and keep the revision-keyed cache usable. Whether the
    provider can resolve siblings is decided separately by _provider_supports_sibling_repo_context.
    """
    sibling_entries = []
    for entry in context_files:
        repo_id, file_path = _parse_repo_context_file_entry(entry)
        if repo_id is not None and file_path:
            sibling_entries.append((repo_id, file_path))
    return sibling_entries


def _parse_repo_context_file_entry(entry: str | dict) -> tuple[str | None, str]:
    """Parse a repo-context entry into (sibling repo id, file path).

    A string entry is always a file path in the current repository and yields (None, path).
    A sibling entry is a structured ``{"repo_id": ..., "file_path": ...}`` dict naming a
    repository in the same namespace/owner (GitLab: ``group/subgroup/project``, GitHub:
    ``owner/repo``); its file is read from the sibling's default branch. A malformed entry
    yields (None, "") and is skipped by the loader.
    """
    if isinstance(entry, dict):
        repo_id = entry.get("repo_id")
        file_path = entry.get("file_path")
        if not isinstance(repo_id, str) or not isinstance(file_path, str):
            return None, ""
        repo_id = repo_id.strip().strip("/")
        file_path = file_path.strip().lstrip("/")
        if not repo_id or not file_path:
            return None, ""
        return repo_id, file_path
    if not isinstance(entry, str):
        return None, ""
    return None, entry.strip()


def _get_provider_repo_context_cache(git_provider) -> _RepoContextCache:
    repo_context_cache = getattr(git_provider, REPO_CONTEXT_CACHE_ATTRIBUTE, None)
    if repo_context_cache is None or not isinstance(repo_context_cache, _RepoContextCache):
        repo_context_cache = _RepoContextCache()
        setattr(git_provider, REPO_CONTEXT_CACHE_ATTRIBUTE, repo_context_cache)
    return repo_context_cache


def _get_cached_repo_context(
    git_provider, context_files: list, max_lines: int, context_ref: str | None
):
    process_cache_key = _get_repo_context_process_cache_key(
        git_provider, context_files, max_lines, context_ref
    )
    if process_cache_key is not None:
        cached_repo_context = _repo_context_process_cache.get(process_cache_key, _REPO_CONTEXT_CACHE_MISS)
        if cached_repo_context is not _REPO_CONTEXT_CACHE_MISS:
            return cached_repo_context

    cache_key = _get_repo_context_cache_key(context_files, max_lines, context_ref)
    cached_repo_context = _get_provider_repo_context_cache(git_provider).get(
        cache_key, _REPO_CONTEXT_CACHE_MISS
    )
    if cached_repo_context is not _REPO_CONTEXT_CACHE_MISS:
        return cached_repo_context

    return _REPO_CONTEXT_CACHE_MISS


def _store_repo_context(
    git_provider, context_files: list, max_lines: int, context_ref: str | None, repo_context: str
) -> None:
    cache_key = _get_repo_context_cache_key(context_files, max_lines, context_ref)
    _get_provider_repo_context_cache(git_provider)[cache_key] = repo_context

    process_cache_key = _get_repo_context_process_cache_key(
        git_provider, context_files, max_lines, context_ref
    )
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


def _read_max_sibling_context_files() -> int:
    try:
        max_siblings = int(get_settings().config.get("repo_context_max_sibling_files",
                                                     _DEFAULT_MAX_SIBLING_CONTEXT_FILES))
    except (TypeError, ValueError):
        max_siblings = _DEFAULT_MAX_SIBLING_CONTEXT_FILES
    # Clamp defensively so a runtime override or an unexpectedly large configured value can never
    # exceed the host-controlled hard ceiling, however the value got into the settings.
    return min(max(0, max_siblings), _HARD_MAX_SIBLING_CONTEXT_FILES)


def _load_repo_context_files(
    git_provider, context_files: list, from_default_branch: bool | None = None
) -> tuple[list[tuple[str, str]], bool]:
    if from_default_branch is None:
        from_default_branch = _read_bool_setting("repo_context_from_default_branch", default=True)
    # Ordered (label, content) entries rather than a label-keyed dict: a local path can equal a
    # sibling's rendered label, and a label-keyed mapping would silently drop one of them.
    files = []
    had_fetch_error = False
    max_siblings = _read_max_sibling_context_files()
    sibling_fetch_attempts = 0
    seen_sibling_pairs = set()
    for entry in context_files:
        repo_id, file_path = _parse_repo_context_file_entry(entry)
        if not file_path:
            if isinstance(entry, str) and not entry.strip():
                get_logger().warning("Skipping invalid repo context file path", artifact={"file_path": entry})
            else:
                get_logger().warning(
                    "Ignoring malformed repo context entry (expected a local file path or a "
                    "{'repo_id': ..., 'file_path': ...} sibling entry)",
                    artifact={"entry": entry},
                )
            continue
        if repo_id is not None:
            if not _provider_supports_sibling_repo_context(git_provider):
                get_logger().warning(
                    f"{type(git_provider).__name__} does not support sibling repository context; "
                    f"skipping {repo_id}:{file_path}"
                )
                continue
            sibling_pair = (repo_id, file_path)
            if sibling_pair in seen_sibling_pairs:
                # Duplicate entries must not consume the fetch cap (or the rendered budget):
                # fetch each unique sibling file once and let the cap bound the real work.
                get_logger().debug(f"Skipping duplicate sibling repo context file: {repo_id}:{file_path}")
                continue
            seen_sibling_pairs.add(sibling_pair)
            if sibling_fetch_attempts >= max_siblings:
                get_logger().warning(
                    f"Stopping repo context at {max_siblings} sibling fetch attempt(s) ("
                    f"config.repo_context_max_sibling_files); skipping {repo_id}:{file_path}"
                )
                continue
            sibling_fetch_attempts += 1
            try:
                content = git_provider.get_sibling_repo_file_content(repo_id, file_path)
            except Exception:
                had_fetch_error = True
                # A sibling fetch can fail after the provider resolved ids, so keep the full
                # traceback; a flat warning hides which request failed and why.
                get_logger().exception(
                    f"Failed to load sibling repo context file: {repo_id}:{file_path}"
                )
                continue
            # Render the file under its sibling path so the model sees where it came from.
            label = f"{repo_id}/{file_path}"
        else:
            if isinstance(entry, str) and _SIBLING_REPO_SEPARATOR in entry:
                # A ':' inside a plain local path used to read as a sibling entry. Structured
                # {"repo_id", "file_path"} dicts are the only sibling form now, so hint at the
                # migration instead of silently splitting on ':'.
                get_logger().warning(
                    "repo context file path contains ':' and is read as a local file; use a "
                    "structured {'repo_id': ..., 'file_path': ...} entry in repo_context_files to "
                    "load a sibling repository file",
                    artifact={"file_path": entry},
                )
            try:
                content = git_provider.get_repo_file_content(file_path, from_default_branch=from_default_branch)
            except Exception as e:
                had_fetch_error = True
                get_logger().warning(f"Failed to load repo context file: {file_path}", artifact={"error": str(e)})
                continue
            label = file_path

        if not content:
            get_logger().debug(f"Repo context file is empty or missing: {label}")
            continue

        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")

        files.append((label, str(content).rstrip()))

    return files, had_fetch_error


def _repo_context_render_entries(files):
    """Yield (path, content) pairs, accepting a label-keyed dict or ordered pairs.

    _load_repo_context_files returns ordered (label, content) pairs so a local path and a
    sibling's rendered label may coexist; a dict spelling is kept for direct callers."""
    return files.items() if isinstance(files, dict) else files


def render_instruction_files(files: dict[str, str] | list[tuple[str, str]]) -> str:
    parts = [
        INSTRUCTION_FILES_INTRO,
        "<instruction_files>",
    ]

    for path, content in _repo_context_render_entries(files):
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


def render_instruction_files_with_line_budget(
    files: dict[str, str] | list[tuple[str, str]], max_lines: int
) -> str:
    parts = [
        INSTRUCTION_FILES_INTRO,
        "<instruction_files>",
    ]
    closing_tag = "</instruction_files>"
    if max_lines < len(parts) + 1:
        return ""

    for path, content in _repo_context_render_entries(files):
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

    # Sibling repositories change independently of the requesting repo, so the current-repo
    # revision cannot reconstruct their default-branch content. Bypass the revision-keyed cache
    # only when the build can actually attempt a sibling fetch. A zero effective sibling-fetch
    # cap disables the fetches entirely, so a local + sibling config with cap 0 must keep the
    # revision-keyed cache usable (otherwise disabled siblings would silently disable local
    # caching too). Malformed entries and providers without sibling support likewise keep the
    # cache usable.
    has_sibling_entries = (
        bool(_sibling_repo_context_entries(context_files))
        and _provider_supports_sibling_repo_context(git_provider)
        and _read_max_sibling_context_files() > 0
    )

    from_default_branch = _read_bool_setting("repo_context_from_default_branch", default=True)
    context_ref = None
    if not has_sibling_entries:
        # Resolve the revision being read once and key the cache on it: within the TTL a rebase
        # or a push to the base branch must not serve file content from a commit that has moved.
        # A sibling-only build never needs this lookup, whose failure would otherwise abort the
        # whole context build before any cross-repository file is loaded.
        context_ref = git_provider.get_repo_context_ref(from_default_branch)
        cached_repo_context = _get_cached_repo_context(
            git_provider, context_files, max_lines, context_ref
        )
        if cached_repo_context is not _REPO_CONTEXT_CACHE_MISS:
            return cached_repo_context

    files, had_fetch_error = _load_repo_context_files(git_provider, context_files, from_default_branch)

    repo_context = render_instruction_files_with_line_budget(files, max_lines) if files else ""

    # Only cache when every file was fetched successfully. A transient/unexpected fetch error must
    # not be cached as a real result, so it is retried instead of being served until the TTL expires.
    if not had_fetch_error and not has_sibling_entries:
        _store_repo_context(git_provider, context_files, max_lines, context_ref, repo_context)
    return repo_context
