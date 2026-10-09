import fnmatch
import re

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

# Keep each glob's subset enumeration small and bound the additional regexes per list.
_MAX_ENUMERATED_GLOBSTARS = 6
_MAX_IGNORE_GLOB_VARIANT_REGEXES = 256
_MAX_EXPANDED_GLOB_LENGTH = 256


def filter_ignored(files, platform = 'github'):
    """Filter out files that match the ignore patterns."""

    try:
        # load regex patterns, and translate glob patterns to regex
        raw_patterns = get_settings().ignore.regex
        patterns = [raw_patterns] if isinstance(raw_patterns, str) else list(raw_patterns)
        glob_setting = get_settings().ignore.glob
        if isinstance(glob_setting, str):  # --ignore.glob=[.*utils.py], --ignore.glob=.*utils.py
            glob_setting = glob_setting.strip('[]').split(",")
        patterns += translate_globs_to_regexes(glob_setting)

        code_generators = get_settings().config.get('ignore_language_framework', [])
        if isinstance(code_generators, str):
            get_logger().warning("'ignore_language_framework' should be a list. Skipping language framework filtering.")
            code_generators = []
        for cg in code_generators:
            glob_patterns = get_settings().generated_code.get(cg, [])
            if isinstance(glob_patterns, str):
                glob_patterns = [glob_patterns]
            patterns += translate_globs_to_regexes(glob_patterns)

        # compile all valid patterns
        compiled_patterns = []
        for r in patterns:
            try:
                compiled_patterns.append(re.compile(r))
            except re.error as e:
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
                if platform in ('github', 'codecommit'):
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
        get_logger().error(
            f'Could not filter file list; filtering did not complete, so the returned list may still '
            f'contain files that the [ignore] rules should have excluded. {e}')

    return files


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
    regexes = {}
    expandable = []
    skipped = variants = 0
    for pattern in dict.fromkeys(globs):
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
    return list(regexes)
