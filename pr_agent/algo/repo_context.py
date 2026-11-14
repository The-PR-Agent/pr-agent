import fnmatch
import posixpath
import re
import time
from collections import OrderedDict
from html import escape
from itertools import islice
from threading import Lock

from pr_agent.algo.prompt_log_guard import prompt_logs_withheld, sanitized_exception_summary
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
# Imports match Claude Code's CLAUDE.md behavior: a path token outside code spans and fenced
# code blocks, resolved relative to the file that contains it.
_MAX_IMPORT_HOPS = 4
_HARD_MAX_IMPORT_FETCHES = 20
_FENCE_START_PATTERN = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_IMPORT_TOKEN_PATTERN = re.compile(r"(?<![\w@._%+-])@")
_URL_IMPORT_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
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
_repo_context_process_cache_lock = Lock()


def _get_markdown_fence(content: str) -> str:
    if MARKDOWN_FENCE not in content:
        return MARKDOWN_FENCE
    min_fence_length = len(MARKDOWN_FENCE)
    pattern = rf"`{{{min_fence_length},}}"
    longest_run = max(match.end() - match.start() for match in re.finditer(pattern, content))
    return "`" * (longest_run + 1)


def _get_repo_context_cache_key(
    context_files: list, max_lines: int, context_ref: str | None, expand_imports: bool = False
) -> tuple[tuple[tuple[str, str], ...], int, str | None, int | None, bool]:
    return (
        tuple((type(file_path).__name__, str(file_path)) for file_path in context_files),
        max_lines,
        context_ref,
        _read_max_glob_matches() if any(
            _is_glob_entry(path) for entry in context_files for path in (entry if isinstance(entry, list) else [entry])
        ) else None,
        expand_imports,
    )


def _get_repo_context_process_cache_key(
    git_provider, context_files: list, max_lines: int, context_ref: str | None, expand_imports: bool = False
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
        context_files, max_lines, context_ref, expand_imports
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


def _blank_markdown_line(line: str) -> str:
    return "".join(character if character in "\r\n" else " " for character in line)


def _mask_fenced_code_blocks(content: str) -> str:
    """Blank fenced code blocks so @tokens inside them are not read as imports."""
    masked = []
    fence_character = None
    fence_length = 0
    for line in content.splitlines(keepends=True):
        if fence_character is None:
            match = _FENCE_START_PATTERN.match(line)
            if match:
                fence = match.group(1)
                fence_character = fence[0]
                fence_length = len(fence)
                masked.append(_blank_markdown_line(line))
            else:
                masked.append(line)
            continue

        masked.append(_blank_markdown_line(line))
        stripped = line.strip()
        if stripped and set(stripped) == {fence_character} and len(stripped) >= fence_length:
            fence_character = None
    return "".join(masked)


def _mask_inline_code_spans(content: str) -> str:
    """Blank Markdown code spans so @tokens inside backticks stay literal."""
    characters = list(content)
    runs = [(match.start(), match.end()) for match in re.finditer(r"`+", content)]
    next_same_length = [None] * len(runs)
    last_by_length = {}
    for position in range(len(runs) - 1, -1, -1):
        run_length = runs[position][1] - runs[position][0]
        next_same_length[position] = last_by_length.get(run_length)
        last_by_length[run_length] = position

    position = 0
    while position < len(runs):
        closing = next_same_length[position]
        if closing is None:
            position += 1
            continue
        for offset in range(runs[position][1], runs[closing][0]):
            if characters[offset] not in "\r\n":
                characters[offset] = " "
        position = closing + 1
    return "".join(characters)


def _iter_markdown_import_paths(content: str):
    """Yield @path tokens outside code, honoring a backslash before spaces."""
    masked = _mask_inline_code_spans(_mask_fenced_code_blocks(content))
    index = 0
    while index < len(masked):
        if masked[index] != "@":
            index += 1
            continue
        match = _IMPORT_TOKEN_PATTERN.match(masked, index)
        if match is None:
            index += 1
            continue
        index = match.end()
        if index >= len(masked) or masked[index] in "\"'":
            continue

        path_characters = []
        while index < len(masked):
            character = masked[index]
            if character == "\\" and index + 1 < len(masked) and masked[index + 1] == " ":
                path_characters.append(" ")
                index += 2
                continue
            if character.isspace():
                break
            path_characters.append(character)
            index += 1

        path = "".join(path_characters)
        if not path or _URL_IMPORT_PATTERN.match(path) or path.startswith("//"):
            continue
        yield path


def _resolve_repo_context_import(parent_path: str, path: str) -> str | None:
    """Resolve an import against its file and reject paths outside the repository."""
    if path.startswith(("/", "~")):
        return None
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(parent_path), path))
    if resolved in (".", "..") or resolved.startswith(("../", "/")):
        return None
    return resolved


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
    git_provider, context_files: list, max_lines: int, context_ref: str | None, expand_imports: bool = False
):
    process_cache_key = _get_repo_context_process_cache_key(
        git_provider, context_files, max_lines, context_ref, expand_imports
    )
    if process_cache_key is not None:
        with _repo_context_process_cache_lock:
            cached_repo_context = _repo_context_process_cache.get(process_cache_key, _REPO_CONTEXT_CACHE_MISS)
        if cached_repo_context is not _REPO_CONTEXT_CACHE_MISS:
            return cached_repo_context

    cache_key = _get_repo_context_cache_key(context_files, max_lines, context_ref, expand_imports)
    cached_repo_context = _get_provider_repo_context_cache(git_provider).get(
        cache_key, _REPO_CONTEXT_CACHE_MISS
    )
    if cached_repo_context is not _REPO_CONTEXT_CACHE_MISS:
        return cached_repo_context

    return _REPO_CONTEXT_CACHE_MISS


def _store_repo_context(
    git_provider, context_files: list, max_lines: int, context_ref: str | None, repo_context: str,
    expand_imports: bool = False,
) -> None:
    cache_key = _get_repo_context_cache_key(context_files, max_lines, context_ref, expand_imports)
    _get_provider_repo_context_cache(git_provider)[cache_key] = repo_context

    process_cache_key = _get_repo_context_process_cache_key(
        git_provider, context_files, max_lines, context_ref, expand_imports
    )
    if process_cache_key:
        with _repo_context_process_cache_lock:
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
            if prompt_logs_withheld():
                get_logger().warning(
                    f"Failed to list repo context directory ({sanitized_exception_summary(e)})"
                )
            else:
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
        self.seen_import_paths = set()
        self.imported_local_paths = set()
        self.rendered_local_paths = set()
        self.import_fetch_attempts = 0
        self.import_limit_warned = False


def _iter_repo_context_imports(
    git_provider, parent_path: str, content: str, from_default_branch: bool,
    state: _RepoContextLoadState, depth: int, seen_local_paths: set[str],
):
    if depth > _MAX_IMPORT_HOPS:
        return
    for path in _iter_markdown_import_paths(content):
        import_path = _resolve_repo_context_import(parent_path, path)
        if (
            import_path is None
            or import_path in state.seen_import_paths
            or import_path in state.rendered_local_paths
            or import_path in seen_local_paths
        ):
            continue
        if state.import_fetch_attempts >= _HARD_MAX_IMPORT_FETCHES:
            if not state.import_limit_warned:
                state.import_limit_warned = True
                get_logger().warning(
                    f"Stopping repo context after {_HARD_MAX_IMPORT_FETCHES} import fetch attempts"
                )
            return
        state.seen_import_paths.add(import_path)
        state.import_fetch_attempts += 1
        try:
            imported_content = git_provider.get_repo_file_content(
                import_path, from_default_branch=from_default_branch
            )
        except Exception as e:
            state.had_fetch_error = True
            if prompt_logs_withheld():
                get_logger().warning(
                    f"Failed to load repo context import ({sanitized_exception_summary(e)})"
                )
            else:
                get_logger().warning(
                    f"Failed to load repo context import: {import_path}", artifact={"error": str(e)}
                )
            continue
        if not imported_content:
            if prompt_logs_withheld():
                get_logger().debug("Repo context import is empty or missing (details withheld)")
            else:
                get_logger().debug(f"Repo context import is empty or missing: {import_path}")
            continue
        if isinstance(imported_content, bytes):
            imported_content = imported_content.decode("utf-8", errors="replace")

        imported_content = str(imported_content)
        state.imported_local_paths.add(import_path)
        state.rendered_local_paths.add(import_path)
        seen_local_paths.add(import_path)
        state.had_content = True
        yield import_path, imported_content
        yield from _iter_repo_context_imports(
            git_provider, import_path, imported_content, from_default_branch, state, depth + 1,
            seen_local_paths,
        )


def _iter_repo_context_files(
    git_provider, context_files: list, from_default_branch: bool | None = None, *, state=None,
    expand_imports: bool | None = None,
):
    if state is None:
        state = _RepoContextLoadState()
    if from_default_branch is None:
        from_default_branch = _read_bool_setting("repo_context_from_default_branch", default=True)
    if expand_imports is None:
        expand_imports = _read_bool_setting("repo_context_expand_imports", default=False)
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
                _, alternative_path = _parse_repo_context_file_entry(alternative)
                if alternative_path in state.imported_local_paths:
                    found_content = True
                    break
                found_content = False
                for label, content in _iter_repo_context_files(
                    git_provider, [alternative], from_default_branch, state=state,
                    expand_imports=expand_imports,
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
            except Exception as e:
                state.had_fetch_error = True
                if prompt_logs_withheld():
                    get_logger().warning(
                        f"Failed to load sibling repo context file ({sanitized_exception_summary(e)})"
                    )
                else:
                    # Preserve traceback diagnostics for ordinary, unguarded callers.
                    get_logger().exception(
                        f"Failed to load sibling repo context file: {repo_id}:{file_path}"
                    )
                continue
            # Render the file under its sibling path so the model sees where it came from.
            label = f"{repo_id}/{file_path}"
        else:
            if file_path in seen_local_paths or file_path in state.imported_local_paths:
                get_logger().debug(f"Skipping duplicate local repo context file: {file_path}")
                continue
            seen_local_paths.add(file_path)
            state.rendered_local_paths.add(file_path)
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
                if prompt_logs_withheld():
                    get_logger().warning(
                        f"Failed to load repo context file ({sanitized_exception_summary(e)})"
                    )
                else:
                    get_logger().warning(f"Failed to load repo context file: {file_path}", artifact={"error": str(e)})
                continue
            label = file_path

        if not content:
            if prompt_logs_withheld():
                get_logger().debug("Repo context file is empty or missing (details withheld)")
            else:
                get_logger().debug(f"Repo context file is empty or missing: {label}")
            continue

        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")

        content = str(content)
        state.had_content = True
        yield label, content
        if expand_imports and repo_id is None:
            yield from _iter_repo_context_imports(
                git_provider, file_path, content, from_default_branch, state, depth=1,
                seen_local_paths=seen_local_paths,
            )


def _repo_context_render_entries(files):
    """Yield (path, content) pairs, accepting a label-keyed dict or ordered pairs.

    Ordered (label, content) pairs let a local path and a sibling's rendered label
    coexist; a dict spelling is kept for direct callers."""
    return files.items() if isinstance(files, dict) else files


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


def build_repo_context(git_provider, *, return_status: bool = False) -> str | tuple[str, bool]:
    """Optionally report whether all attempted instruction fetches succeeded."""
    repo_context_config = _get_repo_context_config()
    if repo_context_config is None:
        return ("", True) if return_status else ""

    context_files, max_lines = repo_context_config
    if not _provider_supports_repo_context(git_provider):
        return ("", True) if return_status else ""

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
    expand_imports = _read_bool_setting("repo_context_expand_imports", default=False)
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
            if prompt_logs_withheld():
                get_logger().warning(
                    f"Failed to resolve the repo context revision ({sanitized_exception_summary(e)})"
                )
            else:
                get_logger().warning(
                    "Failed to resolve the repo context revision; continuing without it",
                    artifact={"error": str(e)},
                )
            context_ref = None
        cached_repo_context = _get_cached_repo_context(
            git_provider, context_files, max_lines, context_ref, expand_imports
        )
        if cached_repo_context is not _REPO_CONTEXT_CACHE_MISS:
            return (cached_repo_context, True) if return_status else cached_repo_context

    load_state = _RepoContextLoadState()
    files = _iter_repo_context_files(
        git_provider, context_files, from_default_branch, state=load_state, expand_imports=expand_imports
    )
    repo_context = render_instruction_files_with_line_budget(files, max_lines, require_next_content_line=True)
    if not load_state.had_content:
        repo_context = ""

    # Cache only when every file that can affect the rendered result was fetched successfully.
    # Leave files beyond the line budget unread.
    if not load_state.had_fetch_error and not has_sibling_entries:
        _store_repo_context(
            git_provider, context_files, max_lines, context_ref, repo_context, expand_imports
        )
    return (repo_context, not load_state.had_fetch_error) if return_status else repo_context
