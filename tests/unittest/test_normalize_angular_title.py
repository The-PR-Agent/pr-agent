import re

import pytest

from pr_agent.tools.pr_description import _normalize_angular_title


TARGET_TITLE_RE = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)"
    r"(\([a-z0-9\-]+\))?: [a-z].{1,70}[^.]$"
)

LONG_SUMMARY = (
    "add SSO support for every identity provider without breaking existing login sessions unexpectedly "
    "after fallback flow"
)

ANGULAR_TITLE_CASES = [
    ("Feature(auth): add SSO support", "feat(auth): add SSO support"),
    ("bug: Fix crash on empty input.", "fix: fix crash on empty input"),
    ("feat(): initial commit", "feat: initial commit"),
    ("feat( ): initial commit", "feat: initial commit"),
    ("feat(User Auth): add SSO", "feat(user-auth): add SSO"),
    ("feat(User_Auth): add SSO", "feat: add SSO"),
    ("chore: ", None),
    ("chore:", None),
    ("WIP: something", None),
    ("feat add SSO", None),
    (f"feat(auth): {LONG_SUMMARY}", "feat(auth): add SSO support for every identity provider without breaking existing"),
    ("feat: hello.", "feat: hello"),
    ("feat: hello?", "feat: hello?"),
    ("feat: hello!", "feat: hello!"),
    ("feat: A", None),
    ("feat: AB", None),
    ("feat: Update API endpoints", "feat: update API endpoints"),
    ("feat:\nadd\nSSO", "feat: add SSO"),
    ("feat:add SSO", "feat: add SSO"),
    ("`feat: add SSO`", "feat: add SSO"),
    ("#feat: add SSO", "feat: add SSO"),
    ("", None),
    ("   ", None),
    ("Docs: update readme.", "docs: update readme"),
    ("Refactoring(api): rename methods", "refactor(api): rename methods"),
]


@pytest.mark.parametrize("raw, expected", ANGULAR_TITLE_CASES)
def test_normalize_angular_title_adversarial_fixtures(raw, expected):
    result = _normalize_angular_title(raw)

    if expected is None:
        assert result is None
        assert result != ""
    else:
        assert result == expected


def test_normalize_angular_title_outputs_match_target_regex():
    for _, expected in ANGULAR_TITLE_CASES:
        if expected is None:
            continue
        assert TARGET_TITLE_RE.fullmatch(expected) is not None
