import re
import time

import regex

from pr_agent.log import get_logger

# Settings patterns can come from a repository's settings file and run on pull request text, so bound them.
USER_REGEX_TIMEOUT_SECONDS = 0.05
USER_REGEX_BUDGET_SECONDS = 1.0
# The timeout does not cover compilation, so bound what compiling can cost first. The length cap matches
# the ignore globs'. `regex` also unrolls counted repeats when it compiles, so the 17 characters
# (?:a{9999}){9999} would take gigabytes; the minimum counts of all `{m}`, `{m,}` and `{m,n}` repeats
# together (a conservative bound for nested ones) may not exceed USER_REGEX_MAX_REPEAT_PRODUCT.
USER_REGEX_MAX_LENGTH = 1024
USER_REGEX_MAX_REPEAT_PRODUCT = 10_000
_COUNTED_REPEAT_RE = re.compile(r"\{(\d+)(?:,\d*)?\}")


def compile_user_regex(pattern):
    """Compile a settings pattern for timed matching, raising re.error like re.compile does.

    A pattern over the length or repeat cap raises re.error too, so each site handles it like an invalid one.
    """
    if isinstance(pattern, str):
        if len(pattern) > USER_REGEX_MAX_LENGTH:
            raise re.error(f"pattern is {len(pattern)} characters long; the limit is {USER_REGEX_MAX_LENGTH}")
        repeats = 1
        for counted in _COUNTED_REPEAT_RE.finditer(pattern):
            repeats *= max(int(counted.group(1)), 1)
            if repeats > USER_REGEX_MAX_REPEAT_PRODUCT:
                raise re.error(f"pattern repeats more than {USER_REGEX_MAX_REPEAT_PRODUCT} times in total")
    re.compile(pattern)
    try:
        return regex.compile(pattern)
    except regex.error as e:
        raise re.error(str(e), pattern) from e


class UserRegexBudget:
    """One time budget shared by the settings-pattern matches of a single decision."""

    def __init__(self, seconds=USER_REGEX_BUDGET_SECONDS):
        self.deadline = time.monotonic() + seconds
        self._timed_out = set()

    @property
    def timed_out(self):
        return bool(self._timed_out)

    def run(self, pattern, method, string, setting):
        """Run ``pattern.<method>(string)`` with a timeout; ``finditer`` matches are returned as a list.

        A match that runs out of time counts as no match: it returns None, logs a warning, and the
        pattern is skipped for the rest of the decision so it cannot use up the budget of the others.
        """
        key = (setting, pattern.pattern)
        if key in self._timed_out:
            return None
        timeout = min(USER_REGEX_TIMEOUT_SECONDS, self.deadline - time.monotonic())
        try:
            if timeout <= 0:
                raise TimeoutError
            if method == "finditer":
                return list(pattern.finditer(string, timeout=timeout))
            return getattr(pattern, method)(string, timeout=timeout)
        except TimeoutError:
            self._timed_out.add(key)
            get_logger().warning(
                f"Pattern in '{setting}' ran out of time and was treated as not matching",
                artifact={"setting": setting, "pattern": pattern.pattern})
            return None
