import fnmatch
import re

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

_GLOBSTAR_SEGMENT = "**/"  # a '**' that starts a path segment and is followed by a separator
_MATCHES_EVERYTHING = fnmatch.translate("*")  # loop-invariant, unlike the glob being translated
# A glob contributes 2**n - 1 zero-directory variants, so the first limit keeps one glob to 64
# regexes, and the second bounds the variants this module multiplies out of all of them:
# filter_ignored compiles and runs every regex it returns against every file, and ignore.glob is
# one of the keys a repository's own .pr_agent.toml may set, so a globstar-heavy list would
# otherwise multiply that work without limit. A glob's own pattern is what the operator configured
# and costs one regex, as it did before, so those are never dropped: dropping one analyzes files
# the operator excluded.
_MAX_ENUMERATED_GLOBSTARS = 6
_MAX_IGNORE_GLOB_VARIANT_REGEXES = 256


def filter_ignored(files, platform = 'github'):
    """Filter out files that match the ignore patterns."""

    try:
        # load regex patterns, and translate glob patterns to regex
        raw_patterns = get_settings().ignore.regex
        patterns = [raw_patterns] if isinstance(raw_patterns, str) else list(raw_patterns)
        glob_setting = get_settings().ignore.glob
        if isinstance(glob_setting, str):  # --ignore.glob=[.*utils.py], --ignore.glob=.*utils.py
            glob_setting = glob_setting.strip('[]').split(",")
        budget = _RegexBudget(_MAX_IGNORE_GLOB_VARIANT_REGEXES)
        patterns += translate_globs_to_regexes(glob_setting, budget)

        code_generators = get_settings().config.get('ignore_language_framework', [])
        if isinstance(code_generators, str):
            get_logger().warning("'ignore_language_framework' should be a list. Skipping language framework filtering.")
            code_generators = []
        for cg in code_generators:
            glob_patterns = get_settings().generated_code.get(cg, [])
            if isinstance(glob_patterns, str):
                glob_patterns = [glob_patterns]
            patterns += translate_globs_to_regexes(glob_patterns, budget)

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


def _class_member_start(pattern: str, class_start: int) -> int:
    """Return the offset of a bracket expression's first member.

    fnmatch treats a leading ``!`` as negation and a leading ``]`` as an ordinary member, so the
    member is not necessarily where the expression can close. The caller skips past it.
    """
    member = class_start + 1
    if pattern[member:member + 1] == "!":
        member += 1
    return member


def _class_end(pattern: str, class_start: int, last_close: int) -> int:
    """Return the offset just past the bracket expression starting at ``class_start``.

    fnmatch reads a leading ``!`` and ``]`` as members rather than as the closing bracket, and
    reads an unterminated ``[`` as a literal, leaving the rest of the pattern to be parsed. The
    first member is skipped unconditionally, because fnmatch tests that character before it looks
    for the closing bracket, so the scan starts one past whichever member it turned out to be.

    ``last_close`` is where the pattern's final ``]`` is, so a bracket that has nothing left to
    close it is recognised without rescanning the rest of the pattern.
    """
    index = _class_member_start(pattern, class_start) + 1
    if index > last_close:  # nothing left for this expression to close
        return class_start + 1
    while True:  # last_close indexes a ']', so the scan stops there at the latest
        if pattern[index] == "]":
            return index + 1
        index += 1


def _globstar_offsets(pattern: str) -> list[int]:
    """Return the offset of every standalone ``**/`` segment in ``pattern``.

    A ``**/`` inside a bracket expression is a literal, and so is an embedded one such as the
    ``**/`` in ``generated**/schema.py``; neither is a globstar segment.
    """
    offsets = []
    index = 0
    last_close = pattern.rfind("]")  # no bracket expression can close past it
    while index < len(pattern):
        if pattern[index] == "[":
            index = _class_end(pattern, index, last_close)
        elif (pattern.startswith(_GLOBSTAR_SEGMENT, index)
                and (index == 0 or pattern[index - 1] == "/")):
            offsets.append(index)
            index += len(_GLOBSTAR_SEGMENT)
        else:
            index += 1
    return offsets


class _RegexBudget:
    """How many zero-directory variants a single ``filter_ignored`` call may add.

    ``translate_globs_to_regexes`` is called once per ignore list, so the budget is held here and
    passed in: a repository with several ``ignore_language_framework`` generators would otherwise
    get a fresh allowance for each of them.
    """

    def __init__(self, limit: int):
        self.limit = limit
        self.kept: list[str] = []  # cumulative and deduplicated; a call takes kept[start:]
        self.retained: set[str] = set()
        self.variants = 0
        self.reported: set[str] = set()

    def retain(self, translation: str) -> None:
        """Keep a pattern this function already produced, or one the operator configured.

        Neither is charged to the budget: both were kept before this change, and dropping one
        analyzes files the operator asked to exclude.
        """
        if translation not in self.retained:
            self.retained.add(translation)
            self.kept.append(translation)

    def retain_variant(self, translation: str) -> bool:
        """Keep one expanded variant; False when the ceiling is already reached."""
        if translation in self.retained:
            return True  # a repeated glob costs nothing to filter again
        if self.variants >= self.limit:
            return False
        self.retained.add(translation)
        self.kept.append(translation)
        self.variants += 1
        return True

    def should_report(self, key: str) -> bool:
        """Claim a condition for this call; False when it was already reported.

        Reporting is left to the caller so the log line carries the frame that decided it.
        """
        if key in self.reported:
            return False
        self.reported.add(key)
        return True


def _warn_match_everything(budget: _RegexBudget, glob: str, form: str) -> None:
    """Report a glob that ignores every file, so an empty review is explained rather than silent.

    One report per call, whichever path finds it: the configured forms are all checked before any
    budget is spent, and a variant can only be retained while there is room for it.
    """
    if not budget.should_report("match-everything"):
        return
    if form == glob:  # the operator wrote it outright, so naming a form would only repeat it
        message = (f"The ignore glob '{glob}' matches every file, so no file will be analyzed; "
                   f"narrow the glob if that was not intended")
        artifact = {"glob": glob}
    else:
        message = (f"The ignore glob '{glob}' matches every file, as '{form}' does, so no file will "
                   f"be analyzed; narrow the glob if that was not intended")
        artifact = {"glob": glob, "form": form}
    get_logger().warning(message, artifact=artifact)


def translate_globs_to_regexes(globs: list, budget: _RegexBudget | None = None):
    """Translate ignore globs, letting a standalone ``**/`` match zero or more directories.

    fnmatch has no globstar: ``*`` already crosses ``/``, so a ``**/`` segment only matches when
    at least one directory is present, and ``src/**/generated_*.py`` never matched
    ``src/generated_pb.py``. Each way its globstars can match zero directories is translated as a
    pattern of its own instead of being folded into one regex with optional groups: nested
    optional groups make a non-matching path backtrack exponentially, and a glob can come from a
    repository's own .pr_agent.toml. Every combination is covered, not only the flattened one, so
    ``a/**/x/**/b.py`` also matches ``a/x/y/b.py``.

    ``budget`` bounds how many variants every call sharing it may add; without one, this call gets
    its own. Every glob's own pattern is always kept, whatever the budget says.
    """
    if budget is None:
        budget = _RegexBudget(_MAX_IGNORE_GLOB_VARIANT_REGEXES)
    # a repeated glob translates to what the first one did, so it never charges the budget and
    # nothing else would stop the loop from expanding it again for every repeat
    globs = list(dict.fromkeys(globs))
    start = len(budget.kept)

    for pattern in globs:  # what the operator configured, before anything is expanded
        # '*' and '**' are the operator writing the everything-matcher outright, so the configured
        # form is checked as well as the root-level one a '**/'-leading glob collapses to
        forms = [pattern]
        if pattern.startswith(_GLOBSTAR_SEGMENT):
            # the root-level form is the coverage this function already had, before the expansion
            # below existed, so it is kept here where expanding an earlier glob cannot reach it
            forms.append(pattern[len(_GLOBSTAR_SEGMENT):])
        for form in forms:
            if not form:  # a glob of nothing but '**/' segments; it matches no file path
                continue
            translation = fnmatch.translate(form)
            budget.retain(translation)
            if translation == _MATCHES_EVERYTHING:
                _warn_match_everything(budget, pattern, form)

    for pattern in globs:
        offsets = _globstar_offsets(pattern)
        if not offsets:
            continue
        if len(offsets) > _MAX_ENUMERATED_GLOBSTARS:
            if budget.should_report("over-cap glob"):
                get_logger().warning(
                    f"Skipping the zero-directory globstar variants of the ignore glob "
                    f"'{pattern}', which has {len(offsets)} '**/' segments; it keeps the form that "
                    f"requires a directory for every '**/' segment"
                    + (", plus its root-level form" if pattern.startswith(_GLOBSTAR_SEGMENT) else ""),
                    artifact={"pattern": pattern, "globstars": len(offsets)})
            continue
        for combination in range(1, 1 << len(offsets)):  # every non-empty subset of globstars
            dropped = {
                offset + within
                for position, offset in enumerate(offsets)
                if combination >> position & 1
                for within in range(len(_GLOBSTAR_SEGMENT))
            }
            variant = "".join(symbol for index, symbol in enumerate(pattern) if index not in dropped)
            if not variant:  # a pattern of nothing but '**/' segments; it matches no file path
                continue
            translation = fnmatch.translate(variant)
            if not budget.retain_variant(translation):
                # how many variants the rest would produce is not knowable without expanding them
                if budget.should_report("reached the ignore-glob limit"):
                    get_logger().warning(
                        f"Ignoring the remaining zero-directory variants of the ignore globs, from "
                        f"'{pattern}' on: they would take the regexes past the {budget.limit} that "
                        f"can be run against every file in the diff, so files only those variants "
                        f"would ignore are still analyzed",
                        artifact={"limit": budget.limit, "variants": budget.variants,
                                  "glob": pattern})
                # a variant that was refused is not kept, so it cannot empty the review and has
                # nothing to report; every configured form was checked before any budget was spent
                return budget.kept[start:]
            if translation == _MATCHES_EVERYTHING:
                _warn_match_everything(budget, pattern, variant)
    return budget.kept[start:]
