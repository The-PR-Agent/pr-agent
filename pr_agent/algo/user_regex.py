import re
import time

import regex

from pr_agent.log import get_logger

# Settings patterns can come from a repository's settings file and run on pull request text, so bound them.
USER_REGEX_TIMEOUT_SECONDS = 0.05
USER_REGEX_BUDGET_SECONDS = 1.0


def compile_user_regex(pattern):
    """Compile a settings pattern for timed matching, raising re.error like re.compile does."""
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
