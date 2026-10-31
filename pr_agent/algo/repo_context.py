import fnmatch
import re
import time
from collections import OrderedDict
from html import escape
from itertools import islice

from pr_agent.algo.setting_utils import _as_bool_setting
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
_GLOB_CHARACTERS = ("*", "?")
_DEFAULT_MAX_GLOB_MATCHES = 20
# Ceiling for repo_context_max_glob_matches: each match is one file fetch, and a repository's
# .pr_agent.toml may set the value.
_HARD_MAX_GLOB_MATCHES = 100
_REPO_CONTEXT_CACHE_MISS = object()
_COMMIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_LINE_BREAK_PATTERN = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")
_NON_WHITESPACE_PATTERN = re.compile(r"\S")
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
    if MARKDOWN_FENCE not in content:
        return MARKDOWN_FENCE
    min_fence_length = len(MARKDOWN_FENCE)
    pattern = rf"`{{{min_fence_length},}}"
    longest_run = max(match.end() - match.start() for match in re.finditer(pattern, content))
    return "`" * (longest_run + 1)


def _get_repo_context_cache_key(
    context_files: list, max_lines: int, context_ref: str | None
) -> tuple[tuple[tuple[str, str], ...], int, str | None, int | None]:
    return (
        tuple((type(file_path).__name__, str(file_path)) for file_path in context_files),
        max_lines,
        context_ref,
        _read_max_glob_matches() if any(
            _is_glob_entry(path) for entry in context_files for path in (entry if isinstance(entry, list) else [entry])
        ) else None,
    )


def _get_repo_context_process_cache_key(
    git_provider, context_files: list, max_lines: int, context_ref: str | None
) -> tuple | None:
    try:
        pr_url = git_provider.get_pr_url()
    except Exception:
        pr_url = getattr(git_provider, "pr_url", None)

    # A branch name keeps its value while the branch moves, so only a commit SHA may key an
    # entry that outlives this provider instance.
    if not pr_url or not (isinstance(context_ref, str) and _COMMIT_SHA_PATTERN.fullmatch(context_ref)):
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


def _is_repo_context_path_allowed(path: str) -> bool:
    """Return whether a repo-context path stays inside the repository."""
    if path.startswith("/"):
        return False
    return all(segment != ".." for segment in path.split("/"))


def _parse_repo_context_file_entry(entry: str | dict) -> tuple[str | None, str]:
    """Parse a repo-context entry into (sibling repo id, file path).

    A string entry is always a file path in the current repository and yields (None, path).
    A sibling entry is a structured ``{"repo_id": ..., "file_path": ...}`` dict naming a
    repository in the same namespace/owner (GitLab: ``group/subgroup/project``, GitHub:
    ``owner/repo``); its file is read from the sibling's default branch. A malformed or
    escaping entry yields (None, "") and is skipped by the loader.
    """
    if isinstance(entry, dict):
        repo_id = entry.get("repo_id")
        file_path = entry.get("file_path")
        if not isinstance(repo_id, str) or not isinstance(file_path, str):
            return None, ""
        repo_id = repo_id.strip().strip("/")
        file_path = file_path.strip()
        if not repo_id or not file_path or not _is_repo_context_path_allowed(file_path):
            return None, ""
        return repo_id, file_path
    if not isinstance(entry, str):
        return None, ""
    file_path = entry.strip()
    if not _is_repo_context_path_allowed(file_path):
        return None, ""
    return None, file_path


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
    value = get_settings().config.get(key, default)
    return _as_bool_setting(value, default)


def _read_max_sibling_context_files() -> int:
    try:
        max_siblings = int(get_settings().config.get("repo_context_max_sibling_files",
                                                     _DEFAULT_MAX_SIBLING_CONTEXT_FILES))
    except (TypeError, ValueError):
        max_siblings = _DEFAULT_MAX_SIBLING_CONTEXT_FILES
    # Clamp defensively so a runtime override or an unexpectedly large configured value can never
    # exceed the host-controlled hard ceiling, however the value got into the settings.
    return min(max(0, max_siblings), _HARD_MAX_SIBLING_CONTEXT_FILES)


def _read_max_glob_matches() -> int:
    try:
        max_matches = int(get_settings().config.get("repo_context_max_glob_matches", _DEFAULT_MAX_GLOB_MATCHES))
    except (TypeError, ValueError):
        max_matches = _DEFAULT_MAX_GLOB_MATCHES
    return min(max(0, max_matches), _HARD_MAX_GLOB_MATCHES)


def _is_glob_entry(entry) -> bool:
    return isinstance(entry, str) and any(character in entry for character in _GLOB_CHARACTERS)


def _expand_glob_entries(git_provider, context_files: list, from_default_branch: bool, *, state):
    """Yield local entries, expanding ``dir/*.md``-style entries into sorted file paths on demand.

    Only the file-name part may hold wildcards, so each entry costs one directory listing.
    Sibling entries are not expanded.
    """
    expanded = []
    max_matches = _read_max_glob_matches()
    for entry in context_files:
        if not _is_glob_entry(entry):
            if not (isinstance(entry, str) and entry.strip().lstrip("/") in expanded):
                expanded.append(entry)
                yield entry
            continue
        raw_entry = entry.strip()
        if not _is_repo_context_path_allowed(raw_entry):
            get_logger().warning(
                "Skipping repo context glob with an absolute path or '..' segment",
                artifact={"pattern": entry},
            )
            continue
        dir_path, _, name_pattern = raw_entry.rpartition("/")
        if any(character in dir_path for character in _GLOB_CHARACTERS) or not name_pattern:
            get_logger().warning(
                "Skipping repo context glob: only the file-name part may contain wildcards",
                artifact={"pattern": entry},
            )
            continue
        try:
            listed = git_provider.list_repo_directory(dir_path, from_default_branch=from_default_branch)
        except Exception as e:
            state.had_fetch_error = True
            get_logger().warning(f"Failed to list repo context directory: {dir_path}", artifact={"error": str(e)})
            continue
        if listed is None:
            get_logger().warning(
                f"{type(git_provider).__name__} cannot list repository directories; skipping repo context glob",
                artifact={"pattern": entry},
            )
            continue
        matches = sorted(path for path in listed if fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], name_pattern))
        if len(matches) > max_matches:
            get_logger().warning(
                f"Repo context glob matched {len(matches)} files; keeping the first {max_matches} "
                "(config.repo_context_max_glob_matches)",
                artifact={"pattern": entry},
            )
            matches = matches[:max_matches]
        for path in matches:
            if path not in expanded:
                expanded.append(path)
                yield path


class _RepoContextLoadState:
    def __init__(self):
        self.had_fetch_error = False
        self.had_content = False


def _iter_repo_context_files(
    git_provider, context_files: list, from_default_branch: bool | None = None, *, state=None
):
    if state is None:
        state = _RepoContextLoadState()
    if from_default_branch is None:
        from_default_branch = _read_bool_setting("repo_context_from_default_branch", default=True)
    context_files = _expand_glob_entries(git_provider, context_files, from_default_branch, state=state)
    # Ordered (label, content) entries rather than a label-keyed dict: a local path can equal a
    # sibling's rendered label, and a label-keyed mapping would silently drop one of them.
    max_siblings = _read_max_sibling_context_files()
    sibling_fetch_attempts = 0
    seen_sibling_pairs = set()
    seen_local_paths = set()
    for entry in context_files:
        if isinstance(entry, list):
            for alternative in entry:
                if not isinstance(alternative, str):
                    get_logger().warning(
                        "Ignoring repo context alternative: alternatives must be local file paths",
                        artifact={"entry": alternative},
                    )
                    continue
                found_content = False
                for label, content in _iter_repo_context_files(
                    git_provider, [alternative], from_default_branch, state=state
                ):
                    found_content = True
                    if label not in seen_local_paths:
                        seen_local_paths.add(label)
                        yield label, content
                if found_content:
                    break
            continue
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
                state.had_fetch_error = True
                # A sibling fetch can fail after the provider resolved ids, so keep the full
                # traceback; a flat warning hides which request failed and why.
                get_logger().exception(
                    f"Failed to load sibling repo context file: {repo_id}:{file_path}"
                )
                continue
            # Render the file under its sibling path so the model sees where it came from.
            label = f"{repo_id}/{file_path}"
        else:
            if file_path in seen_local_paths:
                get_logger().debug(f"Skipping duplicate local repo context file: {file_path}")
                continue
            seen_local_paths.add(file_path)
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
                state.had_fetch_error = True
                get_logger().warning(f"Failed to load repo context file: {file_path}", artifact={"error": str(e)})
                continue
            label = file_path

        if not content:
            get_logger().debug(f"Repo context file is empty or missing: {label}")
            continue

        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")

        state.had_content = True
        yield label, str(content)


def _load_repo_context_files(
    git_provider, context_files: list, from_default_branch: bool | None = None
) -> tuple[list[tuple[str, str]], bool]:
    """Load every configured file for compatibility callers."""
    state = _RepoContextLoadState()
    files = [
        (label, content.rstrip())
        for label, content in _iter_repo_context_files(
            git_provider, context_files, from_default_branch, state=state
        )
    ]
    return files, state.had_fetch_error


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


def _split_instruction_lines(content: str, max_lines: int) -> tuple[list[str], bool]:
    """Keep a bounded prefix with the same line and whitespace rules as rstrip().splitlines()."""
    if max_lines <= 0:
        return [], _NON_WHITESPACE_PATTERN.search(content) is not None

    # Keep native splitting for typical small instruction files.
    if len(content) <= 64 * 1024:
        lines = content.rstrip().splitlines()
        return lines[:max_lines], len(lines) > max_lines

    boundary = next(islice(_LINE_BREAK_PATTERN.finditer(content), max_lines - 1, None), None)
    if boundary is None:
        return content.rstrip().splitlines(), False

    truncated = _NON_WHITESPACE_PATTERN.search(content, boundary.end()) is not None
    prefix = content[:boundary.end()]
    return (prefix if truncated else prefix.rstrip()).splitlines(), truncated


def render_instruction_files_with_line_budget(
    files: dict[str, str] | list[tuple[str, str]], max_lines: int, *, require_next_content_line: bool = False
) -> str:
    parts = [
        INSTRUCTION_FILES_INTRO,
        "<instruction_files>",
    ]
    closing_tag = "</instruction_files>"
    if max_lines < len(parts) + 1:
        return ""

    for path, content in _repo_context_render_entries(files):
        # Reserve two header lines, three footer lines, and the closing instruction tag.
        available_content_lines = max_lines - len(parts) - 6
        if available_content_lines < 0:
            break
        content_lines, truncated = _split_instruction_lines(content, available_content_lines)
        if truncated:
            if not content_lines:
                break
            content_lines[-1] = TRUNCATION_MARKER

        scope = path.rsplit("/", 1)[0] if "/" in path else "repo-root"
        fence = _get_markdown_fence("\n".join(content_lines))
        file_header = [
            f'<file path="{escape(path, quote=True)}" scope="{escape(scope, quote=True)}">',
            f"{fence}markdown",
        ]
        file_footer = [
            fence,
            "</file>",
            "",
        ]
        parts.extend(file_header)
        parts.extend(content_lines)
        parts.extend(file_footer)
        if truncated:
            break

        # The lazy file iterator should not fetch entries that cannot contribute content.
        minimum_next_file_lines = len(file_header) + len(file_footer) + 1
        if require_next_content_line:
            minimum_next_file_lines += 1
        if max_lines - len(parts) < minimum_next_file_lines:
            break

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
        try:
            context_ref = git_provider.get_repo_context_ref(from_default_branch)
        except Exception as e:
            # The revision only keys the cache. Without it the content is read without the
            # revision pinning, which is a weaker guarantee, not a missing one. Repo context
            # is an optional extra, so a provider failure here must not abort the command.
            get_logger().warning(
                "Failed to resolve the repo context revision; continuing without it",
                artifact={"error": str(e)},
            )
            context_ref = None
        cached_repo_context = _get_cached_repo_context(
            git_provider, context_files, max_lines, context_ref
        )
        if cached_repo_context is not _REPO_CONTEXT_CACHE_MISS:
            return cached_repo_context

    load_state = _RepoContextLoadState()
    files = _iter_repo_context_files(
        git_provider, context_files, from_default_branch, state=load_state
    )
    repo_context = render_instruction_files_with_line_budget(files, max_lines, require_next_content_line=True)
    if not load_state.had_content:
        repo_context = ""

    # Cache only when every file that can affect the rendered result was fetched successfully.
    # Leave files beyond the line budget unread.
    if not load_state.had_fetch_error and not has_sibling_entries:
        _store_repo_context(git_provider, context_files, max_lines, context_ref, repo_context)
    return repo_context
