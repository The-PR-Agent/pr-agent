import fnmatch
import re

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger


def filter_ignored(files, platform = 'github'):
    """Filter out files that match the ignore patterns.

    A file is identified by the path it has in the merge result, so a rename is
    filtered by its destination, with its source as the fallback for entries that
    arrive without one. One path is decided per entry up front: testing a path and
    then keeping the entry because its other path did not match let a rename into
    an ignored path through, and its content reached the model.
    """

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
        # With no compiled pattern there is nothing to match against, so the list is
        # returned untouched on every platform.
        if files and compiled_patterns:
            if platform in ('bitbucket', 'gitlab'):
                # GitLab and Bitbucket diff entries name one file by up to two paths
                # (rename source and destination). Resolve the path each entry is
                # filtered by once and keep it paired with the entry while the
                # patterns run: the pairing has to survive every pass, because each
                # pass drops entries and the path of a dropped entry no longer has a
                # file to pair with.
                paired = []
                for f in files:
                    path = _entry_path(f, platform)
                    if path is None:
                        get_logger().debug(
                            "Excluding a diff entry from ignore filtering: it names no path to match on")
                        continue
                    paired.append((f, path))
                for r in compiled_patterns:
                    paired = [(f, path) for f, path in paired if not r.match(path)]
                files = [f for f, _ in paired]
            else:
                for r in compiled_patterns:
                    if platform in ('github', 'codecommit'):
                        files = [f for f in files if (f.filename and not r.match(f.filename))]
                    elif platform == 'bitbucket_server':
                        files = [
                            f for f in files
                            if f.get('path', {}).get('toString') and not r.match(f['path']['toString'])
                        ]
                    elif platform == 'azure':
                        files = [f for f in files if not r.match(f)]
                    elif platform == 'gitea':
                        files = [f for f in files if not r.match(f.get("filename", ""))]
                    elif platform == "gerrit":
                        files_o = []
                        for f in files:
                            path = f.b_path or f.a_path
                            if path and not r.match(path):
                                files_o.append(f)
                        files = files_o

    except Exception as e:
        get_logger().error(f"Could not filter file list: {e}")

    return files


def _entry_path(f, platform) -> str | None:
    """Return the path a diff entry is filtered by, or None when it names none.

    A rename carries a destination (the path the file has in the merge result) and
    a source (where it came from). The destination decides, matching how providers
    label the file, and the source is the fallback for entries that arrive without
    one. A single name has to be chosen here: when the caller instead tested one
    path and, on a match, accepted the file because the *other* path did not match,
    a rename into an ignored path was kept and its content reached the model.
    """
    if platform == 'gitlab':
        candidates = [f.get('new_path'), f.get('old_path')]
    elif platform == 'bitbucket':
        candidates = [getattr(side, 'path', None) for side in (getattr(f, 'new', None), getattr(f, 'old', None))]
    else:
        return None
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def translate_globs_to_regexes(globs: list):
    regexes = []
    for pattern in globs:
        regexes.append(fnmatch.translate(pattern))
        if pattern.startswith("**/"): # cover root-level files
            regexes.append(fnmatch.translate(pattern[3:]))
    return regexes
