import fnmatch
import re
from collections.abc import Mapping

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

# Keep each glob's subset enumeration small and bound the additional regexes per list.
_MAX_ENUMERATED_GLOBSTARS = 6
_MAX_IGNORE_GLOB_VARIANT_REGEXES = 256
_MAX_EXPANDED_GLOB_LENGTH = 256
_MAX_GLOB_LENGTH = 1024


def filter_ignored(files, platform = 'github'):
    """Filter out files that match the ignore patterns."""
    return filter_ignored_with_status(files, platform)[0]


def filter_ignored_with_status(files, platform='github'):
    """Return the best-effort file list and whether all ignore rules were evaluated."""
    complete = platform in (
        'github', 'codecommit', 'local', 'plain_diff', 'mosaico', 'bitbucket',
        'bitbucket_server', 'gitlab', 'azure', 'gitea', 'gerrit',
    )
    try:
        # load regex patterns, and translate glob patterns to regex
        patterns, regex_complete = _setting_as_list(get_settings().ignore.regex, 'ignore.regex')
        glob_setting = get_settings().ignore.glob
        if isinstance(glob_setting, str):  # --ignore.glob=[.*utils.py], --ignore.glob=.*utils.py
            glob_setting = split_glob_patterns(glob_setting)
        glob_list, glob_setting_complete = _setting_as_list(glob_setting, 'ignore.glob')
        glob_patterns, glob_complete = _translate_globs_to_regexes_with_status(glob_list)
        patterns += glob_patterns
        complete = complete and regex_complete and glob_setting_complete and glob_complete

        code_generators = get_settings().config.get('ignore_language_framework', [])
        if not isinstance(code_generators, (list, tuple)):
            complete = False
        if isinstance(code_generators, str):
            get_logger().warning("'ignore_language_framework' should be a list. Skipping language framework filtering.")
            code_generators = []
        code_generators, generators_complete = _setting_as_list(code_generators, 'config.ignore_language_framework')
        complete = complete and generators_complete
        for cg in code_generators:
            if not isinstance(cg, str):
                complete = False
                continue
            glob_list, glob_setting_complete = _setting_as_list(
                get_settings().generated_code.get(cg, []), f'generated_code.{cg}')
            generated_patterns, generated_complete = _translate_globs_to_regexes_with_status(glob_list)
            patterns += generated_patterns
            complete = complete and glob_setting_complete and generated_complete

        # compile all valid patterns
        compiled_patterns = []
        for r in patterns:
            if not isinstance(r, str):
                complete = False
                get_logger().warning(
                    "Skipping non-string ignore regex; expected a string. Other ignore rules still apply",
                    artifact={"type": type(r).__name__})
                continue
            try:
                compiled_patterns.append(re.compile(r))
            except re.error as e:
                complete = False
                get_logger().warning(
                    "Skipping invalid ignore pattern; files it was meant to exclude will be "
                    "sent to the model", artifact={"pattern": r, "error": str(e)})

        # Materialize GitHub incremental dict_values and other iterable file views
        # before applying the same ignore filtering as full-review lists.
        if files and not isinstance(files, list):
            files = list(files)

        # keep filenames that _don't_ match the ignore regex
        if files:
            for r in compiled_patterns:
                if platform in ('github', 'codecommit', 'local', 'plain_diff', 'mosaico'):
                    files = [f for f in files if (f.filename and not r.match(f.filename))]
                elif platform == 'bitbucket':
                    files_o = []
                    for f in files:
                        new, old = getattr(f, 'new', None), getattr(f, 'old', None)
                        path = (new and new.path) or (old and old.path)
                        if path and not r.match(path):
                            files_o.append(f)
                    files = files_o
                elif platform == 'bitbucket_server':
                    files = [
                        f for f in files
                        if f.get('path', {}).get('toString') and not r.match(f['path']['toString'])
                    ]
                elif platform == 'gitlab':
                    files_o = []
                    for f in files:
                        path = f.get('new_path') or f.get('old_path')
                        if path and not r.match(path):
                            files_o.append(f)
                    files = files_o
                elif platform == 'azure':
                    # Azure DevOps returns item paths with a leading slash ("/src/app.cs").
                    # The patterns are anchored, so strip it before matching; otherwise no
                    # pattern ever matches and [ignore] is inert on Azure.
                    files = [f for f in files if not r.match(f.lstrip('/'))]
                elif platform == 'gitea':
                    files = [f for f in files if not r.match(f.get("filename", ""))]
                elif platform == "gerrit":
                    files_o = []
                    for f in files:
                        path = f.b_path or f.a_path
                        if path and not r.match(path):
                            files_o.append(f)
                    files = files_o
                else:
                    get_logger().warning(
                        f'No ignore filtering is implemented for platform {platform!r}, so all '
                        f'{len(files)} changed file(s) are being sent to the model.',
                        artifact={'platform': platform, 'file_count': len(files)})
                    break


    except Exception as e:
        complete = False
        get_logger().error(
            f'Could not filter file list; filtering did not complete, so the returned list may still '
            f'contain files that the [ignore] rules should have excluded. {e}')

    return files, complete


def _setting_as_list(value, setting_name: str) -> tuple[list, bool]:
    """Return an ignore setting as a list, and whether it had the expected type.

    A string or a list is used as is. A table keeps its keys but is reported as
    incomplete. Any other value (``42``, ``None``) is dropped with a warning, so
    the remaining ignore rules still apply instead of the whole filter pass failing.
    """
    if isinstance(value, str):
        return [value], True
    if isinstance(value, (list, tuple)):
        return list(value), True
    if isinstance(value, Mapping):
        return list(value), False
    get_logger().warning(
        f"Skipping '{setting_name}'; expected a string or a list of strings. Other ignore rules still apply",
        artifact={"setting": setting_name, "type": type(value).__name__})
    return [], False


def split_glob_patterns(value: str) -> list[str]:
    """Split a string ``ignore.glob`` value into glob patterns.

    A bracketed value is a list wrapper, so one leading and one trailing
    bracket are removed. A comma inside a bracket expression belongs to the
    pattern, so only top-level commas separate patterns.
    """
    text = value.strip()
    if text.startswith("[") and text.endswith("]") and len(text) >= 2:
        text = text[1:-1]

    patterns = []
    start = index = 0
    last_close = text.rfind("]")
    while index < len(text):
        if text[index] == "[":
            end = index + 2 + (text[index + 1:index + 2] == "!")
            if end <= last_close:
                index = text.index("]", end)
        elif text[index] == ",":
            patterns.append(text[start:index])
            start = index + 1
        index += 1
    patterns.append(text[start:])

    return [pattern.strip() for pattern in patterns if pattern.strip()]


def _globstar_offsets(pattern: str) -> list[int]:
    """Locate whole ``**/`` segments outside fnmatch bracket expressions."""
    offsets = []
    index = 0
    last_close = pattern.rfind("]")
    while index < len(pattern):
        if pattern[index] == "[":
            end = index + 1
            if pattern[end:end + 1] == "!":
                end += 1
            end += 1  # fnmatch treats a leading ']' as a member, not the closing bracket
            if end <= last_close:
                while pattern[end] != "]":
                    end += 1
                index = end + 1
            else:
                index += 1  # an unclosed '[' is literal; continue scanning after it
        elif pattern.startswith("**/", index) and (index == 0 or pattern[index - 1] == "/"):
            offsets.append(index)
            index += 3
        else:
            index += 1
    return offsets


def translate_globs_to_regexes(globs: list):
    """Expand standalone ``**/`` segments into separate zero-directory translations."""
    return _translate_globs_to_regexes_with_status(globs)[0]


def _translate_globs_to_regexes_with_status(globs: list):
    """Translate globs without hiding bounded omissions from metadata callers."""
    complete = not isinstance(globs, Mapping)
    regexes = {}
    expandable = []
    seen = set()
    skipped = skipped_long = variants = 0
    for pattern in globs:
        if not isinstance(pattern, str):
            complete = False
            get_logger().warning(
                "Skipping non-string ignore glob; expected a string. Other ignore rules still apply",
                artifact={"type": type(pattern).__name__})
            continue
        if pattern in seen:
            continue
        seen.add(pattern)
        if len(pattern) > _MAX_GLOB_LENGTH:
            skipped_long += 1
            continue
        # Keep every configured glob and its existing root-level form before spending any quota.
        forms = [pattern, pattern[3:]] if pattern.startswith("**/") else [pattern]
        for form in forms:
            if form:
                regexes[fnmatch.translate(form)] = None
        offsets = _globstar_offsets(pattern)
        if len(offsets) > _MAX_ENUMERATED_GLOBSTARS or (offsets and len(pattern) > _MAX_EXPANDED_GLOB_LENGTH):
            skipped += 1
        elif offsets:
            expandable.append((pattern.split("/"), [pattern[:offset].count("/") for offset in offsets]))

    for segments, globstars in expandable:
        for mask in range(1, 1 << len(globstars)):
            dropped = {index for bit, index in enumerate(globstars) if mask >> bit & 1}
            variant = "/".join(segment for index, segment in enumerate(segments) if index not in dropped)
            if not variant:
                continue
            translation = fnmatch.translate(variant)
            if translation in regexes:
                continue
            if variants >= _MAX_IGNORE_GLOB_VARIANT_REGEXES:
                skipped += 1
                break
            regexes[translation] = None
            variants += 1
    if skipped:
        get_logger().warning(
            "Skipped zero-directory '**/' variants of some ignore globs; files only those "
            "variants match are still analyzed", artifact={"globs": skipped})
    if skipped_long:
        get_logger().warning(
            f"Skipped {skipped_long} ignore glob(s) longer than {_MAX_GLOB_LENGTH} characters",
            artifact={"globs": skipped_long, "max_length": _MAX_GLOB_LENGTH})
    return list(regexes), complete and not (skipped or skipped_long)
