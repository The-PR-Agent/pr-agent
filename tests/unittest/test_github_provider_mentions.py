from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pr_agent.algo.inline_comment_dedup import (
    body_fingerprint,
    has_marker,
    key_issue_body_with_markers,
    marker_fingerprints,
)
from pr_agent.algo.mentions import neutralize_github_mentions
from pr_agent.git_providers import github_provider as gh_module
from pr_agent.git_providers.github_provider import MAX_PR_TITLE_GITHUB_LENGTH, GithubProvider

ZWSP = "\u200b"
TEXT = "Ask @octocat and @org/security-team to check `@decorator`, or mail x@y.com."
NEUTRAL = f"Ask @{ZWSP}octocat and @{ZWSP}org/security-team to check `@decorator`, or mail x@y.com."


def _make_provider(max_chars=65000):
    provider = GithubProvider.__new__(GithubProvider)
    provider.pr = MagicMock()
    provider.issue_main = None
    provider.repo = "owner/repo"
    provider.pr_num = 1
    provider.max_comment_chars = max_chars
    provider.last_commit_id = SimpleNamespace(sha="deadbeef")
    provider.diff_files = []
    provider.base_url = "https://api.github.com"
    return provider


def _use_settings(monkeypatch, persistent):
    settings = SimpleNamespace(
        get=lambda key, default=None: persistent if key == "config.persistent_inline_comments" else default,
        github=SimpleNamespace(try_fix_invalid_inline_comments=False),
    )
    monkeypatch.setattr(gh_module, "get_settings", lambda: settings)


def _reviewed_bodies(provider):
    return [comment["body"] for comment in provider.pr.create_review.call_args.kwargs["comments"]]


def test_publish_comment_neutralizes_mentions_on_a_pr():
    provider = _make_provider()

    provider.publish_comment(TEXT)

    provider.pr.create_issue_comment.assert_called_once_with(NEUTRAL)


def test_publish_comment_neutralizes_mentions_on_an_issue():
    provider = _make_provider()
    provider.issue_main = MagicMock()

    provider.publish_comment(TEXT)

    provider.issue_main.create_comment.assert_called_once_with(NEUTRAL)


def test_edit_comment_neutralizes_mentions():
    provider = _make_provider()
    comment = MagicMock()

    provider.edit_comment(comment, TEXT)

    comment.edit.assert_called_once_with(body=NEUTRAL)


def test_reply_neutralizes_mentions():
    provider = _make_provider()
    provider.pr._requester.requestJsonAndCheck.return_value = ({}, {})

    provider.reply_to_comment_from_comment_id(42, TEXT)

    assert provider.pr._requester.requestJsonAndCheck.call_args.kwargs["input"] == {"body": NEUTRAL}


def test_publish_description_neutralizes_title_and_body():
    provider = _make_provider()

    provider.publish_description("Ping @reviewer", TEXT)

    provider.pr.edit.assert_called_once_with(title=f"Ping @{ZWSP}reviewer", body=NEUTRAL)


def test_publish_description_caps_a_title_grown_by_neutralization():
    provider = _make_provider()
    title = "@a " * (MAX_PR_TITLE_GITHUB_LENGTH // 3)

    provider.publish_description(title, "body")

    published = provider.pr.edit.call_args.kwargs["title"]
    assert len(title) <= MAX_PR_TITLE_GITHUB_LENGTH < len(title.replace("@", "@" + ZWSP))
    assert len(published) == MAX_PR_TITLE_GITHUB_LENGTH
    assert published.startswith(f"@{ZWSP}a ")


@pytest.mark.parametrize("persistent", [False, True])
def test_inline_review_neutralizes_mentions(monkeypatch, persistent):
    _use_settings(monkeypatch, persistent)
    monkeypatch.setattr(gh_module, "get_inline_comment_store", lambda provider: MagicMock(seen=lambda fp: False))
    provider = _make_provider()

    provider.publish_inline_comments([{"path": "src/a.py", "line": 3, "body": TEXT, "_dedup_code_fp": None}])

    body = _reviewed_bodies(provider)[0]
    assert body.startswith(NEUTRAL)
    assert has_marker(body) is persistent
    assert "_dedup_code_fp" not in provider.pr.create_review.call_args.kwargs["comments"][0]


def test_persistent_fingerprint_is_taken_before_neutralizing(monkeypatch):
    _use_settings(monkeypatch, True)
    monkeypatch.setattr(gh_module, "get_inline_comment_store", lambda provider: MagicMock(seen=lambda fp: False))
    provider = _make_provider()

    provider.publish_inline_comments([{"path": "src/a.py", "line": 3, "body": TEXT}])

    assert body_fingerprint("src/a.py", None, TEXT) in marker_fingerprints(_reviewed_bodies(provider)[0])


def test_premarked_key_issue_body_is_neutralized(monkeypatch):
    _use_settings(monkeypatch, True)
    monkeypatch.setattr(gh_module, "get_inline_comment_store", lambda provider: MagicMock(seen=lambda fp: False))
    provider = _make_provider()
    marked = key_issue_body_with_markers(TEXT, "a" * 12, "b" * 12)

    provider.publish_inline_comments([{"path": "src/a.py", "line": 3, "body": marked}])

    body = _reviewed_bodies(provider)[0]
    assert body.startswith(NEUTRAL)
    assert marker_fingerprints(body) == {"a" * 12, "b" * 12}


def test_fallback_republish_does_not_neutralize_twice(monkeypatch):
    _use_settings(monkeypatch, False)
    provider = _make_provider()

    provider.publish_inline_comments([{"path": "src/a.py", "line": 3, "body": NEUTRAL}], disable_fallback=True)

    assert _reviewed_bodies(provider) == [NEUTRAL]


def test_file_level_comment_neutralizes_mentions(monkeypatch):
    monkeypatch.setattr(gh_module, "find_line_number_of_relevant_line_in_file", lambda *args: (-1, -1))
    provider = _make_provider()

    provider.publish_inline_comment(TEXT, "src/a.py", "missing line")

    assert provider.pr.create_review_comment.call_args.args[0] == NEUTRAL


@pytest.mark.parametrize("with_markers", [False, True])
def test_inline_body_grown_by_neutralization_stays_within_the_limit(monkeypatch, with_markers):
    _use_settings(monkeypatch, False)
    max_chars = 200
    provider = _make_provider(max_chars=max_chars)
    text = "@a " * 80
    body = key_issue_body_with_markers(text, "a" * 12, "b" * 12, max_chars) if with_markers else text[:max_chars]

    provider.publish_inline_comments([{"path": "src/a.py", "line": 3, "body": body}])

    published = _reviewed_bodies(provider)[0]
    assert len(published) <= max_chars
    assert published.startswith(f"@{ZWSP}a @{ZWSP}a ")
    assert marker_fingerprints(published) == ({"a" * 12, "b" * 12} if with_markers else set())


def test_publish_comment_keeps_a_blockquoted_fence_intact():
    provider = _make_provider()
    text = "> ```python\n> @decorator\n> ```\n> cc @octocat"

    provider.publish_comment(text)

    provider.pr.create_issue_comment.assert_called_once_with(f"> ```python\n> @decorator\n> ```\n> cc @{ZWSP}octocat")


@pytest.mark.parametrize("text", [
    ">> ```\n>> @x\n>> ```",
    "> > ~~~\n> > @x\n> > ~~~",
    ">>>>```\n>>>>@x\n>>>>```",
    "   > ```\n   >@x\n   > ```",
    ">\t```\n>\t@x\n>\t```",
    "> ```\n>\n> @x\n> ```",
    "- ```\n  @x\n  ```",
    "- > ```\n  > @x\n  > ```",
    "1. > ```\n   > @x\n   > ```",
    "> - ```\n>   @x\n>   ```",
    "- a\n  - b\n\n    ```\n    @x\n    ```",
    ">     @x",
    "# h\n    @x",
    "> ```\n> a\n```\n@x",
    "| h | i |\n|---|---|\n    @x",
])
def test_code_inside_containers_is_left_untouched(text):
    assert neutralize_github_mentions(text) == text


@pytest.mark.parametrize(("text", "expected"), [
    ("> ```\n> @x\n> ```\n@a", f"> ```\n> @x\n> ```\n@{ZWSP}a"),
    ("> ```\n\n> @a", f"> ```\n\n> @{ZWSP}a"),
    ("> ```\n@a", f"> ```\n@{ZWSP}a"),
    ("> text\n>     @a", f"> text\n>     @{ZWSP}a"),
    ("> a `x\n> @lit` b @a", f"> a `x\n> @lit` b @{ZWSP}a"),
    ("> text\n> 2. @a", f"> text\n> 2. @{ZWSP}a"),
    ("-\n\n    @x", "-\n\n    @x"),
    ("-\n  \n    @a", f"-\n  \n    @{ZWSP}a"),
    ("|---|---|\n    @a", f"|---|---|\n    @{ZWSP}a"),
    ("| h |\n|---|---|\n    @a", f"| h |\n|---|---|\n    @{ZWSP}a"),
])
def test_container_boundaries_follow_commonmark(text, expected):
    assert neutralize_github_mentions(text) == expected


@pytest.mark.parametrize("prefix", [
    "- " * 50000,
    "* " * 50000,
    "1. " * 50000,
    "> " * 50000,
    "- " * 50000 + "x\n" + "  " * 50000,
    "- " * 50000 + "x" + "\n" * 50000,
    "- " * 50000 + "> x" + "\n" * 50000,
])
def test_deeply_nested_containers_stay_linear(prefix):
    assert neutralize_github_mentions(prefix + "@a") == f"{prefix}@{ZWSP}a"


@pytest.mark.parametrize(("text", "expected"), [
    ("cc @&#111;ctocat", f"cc @{ZWSP}&#111;ctocat"),
    ("cc @&#x6F;ctocat", f"cc @{ZWSP}&#x6F;ctocat"),
    ("cc &#64;&#111;ctocat", f"cc &#64;{ZWSP}&#111;ctocat"),
    ("cc @&lowbar;x", f"cc @{ZWSP}&lowbar;x"),
    ("a @&amp; b, @&#0;x and @&#38;#111;", "a @&amp; b, @&#0;x and @&#38;#111;"),
    (f"cc @{ZWSP}&#111;ctocat", f"cc @{ZWSP}&#111;ctocat"),
])
def test_entity_encoded_handle_starts_are_neutralized(text, expected):
    assert neutralize_github_mentions(text) == expected


@pytest.mark.parametrize("text", [
    "see https://medium.com/@user",
    "see HTTPS://medium.com/@user",
    "see www.medium.com/@user",
    "see (www.medium.com/@user)",
    "see <https://host/@user>",
    "see <HTTP://host/@user>",
    "see <mailto:@user>",
])
def test_github_autolinks_keep_their_mentions(text):
    assert neutralize_github_mentions(text) == text


@pytest.mark.parametrize(("text", "expected"), [
    ("custom://@octocat", f"custom://@{ZWSP}octocat"),
    ("ftp://x/@octocat", f"ftp://x/@{ZWSP}octocat"),
    ("<ab://x/@octocat>", f"<ab://x/@{ZWSP}octocat>"),
    ("<ftp://x/@octocat>", f"<ftp://x/@{ZWSP}octocat>"),
    ("xhttps://host.org/@user", f"xhttps://host.org/@{ZWSP}user"),
    ("https://@octocat", f"https://@{ZWSP}octocat"),
    ("https://a.b_c.org/@user", f"https://a.b_c.org/@{ZWSP}user"),
    ("awww.host.org/@user", f"awww.host.org/@{ZWSP}user"),
])
def test_text_github_does_not_autolink_is_neutralized(text, expected):
    assert neutralize_github_mentions(text) == expected
