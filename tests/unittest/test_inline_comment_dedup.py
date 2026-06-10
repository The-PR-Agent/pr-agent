from unittest.mock import MagicMock, patch

from pr_agent.algo import inline_comment_dedup as d
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.git_providers.gitlab_provider import GitLabProvider

_FLAG = "config.persistent_inline_comments"


def _flag_side_effect(value=True):
    def _get(key, default=None):
        return value if key == _FLAG else default
    return _get


# --------------------------------------------------------------------------- #
# fingerprints + markers
# --------------------------------------------------------------------------- #
def test_body_fingerprint_strips_lead_and_tag():
    a = d.body_fingerprint(
        "f.py", 10,
        "**Suggestion:** Do the thing [possible issue, importance: 7]")
    b = d.body_fingerprint("f.py", 10, "Do the thing")
    assert a == b
    assert len(a) == 12


def test_body_fingerprint_varies_by_file_and_line():
    assert d.body_fingerprint("f.py", 10, "x") != d.body_fingerprint("g.py", 10, "x")
    assert d.body_fingerprint("f.py", 10, "x") != d.body_fingerprint("f.py", 11, "x")


def test_code_fingerprint_none_without_block():
    assert d.code_fingerprint("f.py", 1, "no code here") is None
    assert d.code_fingerprint("f.py", 1, "```suggestion\n\n```") is None  # empty block


def test_code_fingerprint_whitespace_insensitive():
    fp1 = d.code_fingerprint("f.py", 1, "prose\n```suggestion\nfoo = 1\n```\n")
    fp2 = d.code_fingerprint("f.py", 1, "different prose\n```suggestion\n  foo   = 1  \n```")
    assert fp1 == fp2 and len(fp1) == 12


def test_build_markers():
    assert d.build_markers("aaaaaaaaaaaa", None) == "<!-- pr-agent-dedup: aaaaaaaaaaaa -->"
    out = d.build_markers("aaaaaaaaaaaa", "bbbbbbbbbbbb")
    assert "<!-- pr-agent-dedup: aaaaaaaaaaaa -->" in out
    assert "<!-- pr-agent-dedup-code: bbbbbbbbbbbb -->" in out


def test_inline_comment_line_prefers_line():
    assert d.inline_comment_line({"line": 5, "position": 9}) == 5
    assert d.inline_comment_line({"position": 9}) == 9
    assert d.inline_comment_line({}) is None


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
class _GHComment:
    def __init__(self, body):
        self.body = body


def _gh_provider(existing_bodies):
    """Real GithubProvider instance with only the attributes the dedup path touches."""
    p = GithubProvider.__new__(GithubProvider)
    p.pr = MagicMock()
    p.pr.get_comments.return_value = [_GHComment(b) for b in existing_bodies]
    p.last_commit_id = "deadbeef"
    return p


def test_store_scans_both_marker_forms():
    fp_body = d.body_fingerprint("a.py", 1, "alpha")
    fp_code = d.code_fingerprint("a.py", 1, "p\n```suggestion\nx = 1\n```")
    prov = _gh_provider([
        f"alpha\n\n<!-- pr-agent-dedup: {fp_body} -->",
        f"beta\n\n<!-- pr-agent-dedup-code: {fp_code} -->",
    ])
    store = d.InlineCommentStore(prov)
    assert store.seen(fp_body)
    assert store.seen(fp_code)
    assert not store.seen("ffffffffffff")
    assert store.seen(None) is False


def test_store_load_failure_degrades_to_empty():
    prov = _gh_provider([])
    prov.pr.get_comments.side_effect = RuntimeError("api down")
    store = d.InlineCommentStore(prov)
    assert store.load() == set()  # must not raise


def test_iter_unsupported_provider_raises():
    class FooProvider:
        pass
    try:
        list(d.iter_existing_inline_comment_bodies(FooProvider()))
        assert False, "expected NotImplementedError"
    except NotImplementedError:
        pass


# --------------------------------------------------------------------------- #
# GitHub provider integration
# --------------------------------------------------------------------------- #
def _patch_flag(value):
    gs = patch("pr_agent.git_providers.github_provider.get_settings")
    m = gs.start()
    m.return_value.get.side_effect = _flag_side_effect(value)
    return gs


def test_github_filters_seen_and_marks_new():
    seen_fp = d.body_fingerprint("a.py", None, "old body")
    p = _gh_provider([f"old body\n\n<!-- pr-agent-dedup: {seen_fp} -->"])
    gs = _patch_flag(True)
    try:
        p.publish_inline_comments([
            {"path": "a.py", "line": 10, "body": "old body"},     # duplicate -> dropped
            {"path": "b.py", "line": 20, "body": "new body"},     # new -> kept + marker
        ])
    finally:
        gs.stop()
    published = p.pr.create_review.call_args.kwargs["comments"]
    assert len(published) == 1
    assert published[0]["path"] == "b.py"
    assert "<!-- pr-agent-dedup:" in published[0]["body"]


def test_github_all_duplicates_skips_publish():
    seen_fp = d.body_fingerprint("a.py", None, "old body")
    p = _gh_provider([f"old body\n\n<!-- pr-agent-dedup: {seen_fp} -->"])
    gs = _patch_flag(True)
    try:
        p.publish_inline_comments([{"path": "a.py", "line": 10, "body": "old body"}])
    finally:
        gs.stop()
    p.pr.create_review.assert_not_called()


def test_github_within_batch_duplicate_dropped():
    p = _gh_provider([])
    gs = _patch_flag(True)
    try:
        p.publish_inline_comments([
            {"path": "a.py", "line": 10, "body": "same finding"},
            {"path": "a.py", "line": 10, "body": "same finding"},
        ])
    finally:
        gs.stop()
    published = p.pr.create_review.call_args.kwargs["comments"]
    assert len(published) == 1


def test_github_flag_off_publishes_unmarked():
    p = _gh_provider([])
    gs = _patch_flag(False)
    try:
        p.publish_inline_comments([{"path": "a.py", "line": 10, "body": "x"}])
    finally:
        gs.stop()
    published = p.pr.create_review.call_args.kwargs["comments"]
    assert len(published) == 1
    assert "pr-agent-dedup" not in published[0]["body"]


# --------------------------------------------------------------------------- #
# GitLab provider integration
# --------------------------------------------------------------------------- #
class _FakeDiff:
    base_commit_sha = "base"
    start_commit_sha = "start"
    head_commit_sha = "head"


class _FakeTargetFile:
    filename = "a.py"
    old_filename = "a.py"


def _gl_provider(existing_bodies):
    p = GitLabProvider.__new__(GitLabProvider)
    p.id_mr = 1
    p.mr = MagicMock()
    # existing discussions feed the store's marker scan
    discs = []
    for b in existing_bodies:
        disc = MagicMock()
        disc.attributes = {"notes": [{"body": b}]}
        discs.append(disc)
    p.mr.discussions.list.return_value = discs
    p.mr.notes.list.return_value = []
    p.get_relevant_diff = MagicMock(return_value=_FakeDiff())
    return p


def _send(p, body, edit_type="addition"):
    p.send_inline_comment(
        body=body, edit_type=edit_type, found=True,
        relevant_file="a.py", relevant_line_in_file="+x = 1",
        source_line_no=10, target_file=_FakeTargetFile(), target_line_no=10,
        original_suggestion=None,
    )


def test_gitlab_posts_new_with_marker_and_skips_duplicate():
    p = _gl_provider([])
    gs = patch("pr_agent.git_providers.gitlab_provider.get_settings")
    m = gs.start()
    m.return_value.get.side_effect = _flag_side_effect(True)
    try:
        _send(p, "**Suggestion:** fix it [possible issue, importance: 7]")
        first = p.mr.discussions.create.call_args.args[0]
        assert "<!-- pr-agent-dedup:" in first["body"]
        # same suggestion again in the same run -> recorded in store -> skipped
        _send(p, "**Suggestion:** fix it [possible issue, importance: 7]")
        assert p.mr.discussions.create.call_count == 1
    finally:
        gs.stop()


def test_gitlab_flag_off_posts_unmarked():
    p = _gl_provider([])
    gs = patch("pr_agent.git_providers.gitlab_provider.get_settings")
    m = gs.start()
    m.return_value.get.side_effect = _flag_side_effect(False)
    try:
        _send(p, "plain body")
        body = p.mr.discussions.create.call_args.args[0]["body"]
        assert "pr-agent-dedup" not in body
    finally:
        gs.stop()


# --------------------------------------------------------------------------- #
# regression cases added after review
# --------------------------------------------------------------------------- #
def test_body_fingerprint_strips_non_standard_label():
    # hyphen/digit/capitalised labels must still be stripped so the body
    # fingerprint is stable across runs (review finding: tag regex too narrow)
    a = d.body_fingerprint(
        "f.py", 10,
        "**Suggestion:** Do the thing [best-practice, importance: 3]")
    b = d.body_fingerprint("f.py", 10, "Do the thing")
    assert a == b


def test_github_code_fingerprint_or_match_across_runs():
    # existing comment carries ONLY a code marker; a new comment with different
    # prose but the same suggestion block must be dropped via the code fp even
    # though its body fingerprint differs.
    code_fp = d.code_fingerprint("a.py", None, "p\n```suggestion\nx = 1\n```")
    p = _gh_provider([f"earlier wording\n\n<!-- pr-agent-dedup-code: {code_fp} -->"])
    gs = _patch_flag(True)
    try:
        p.publish_inline_comments([
            {"path": "a.py", "line": 10,
             "body": "totally different wording\n```suggestion\nx = 1\n```"},
        ])
    finally:
        gs.stop()
    p.pr.create_review.assert_not_called()


def test_github_preserves_code_fingerprint_when_hunk_validation_replaces_suggestion_block():
    original_body = "**Suggestion:** fix it\n```suggestion\nx = 1\n```"
    code_fp = d.code_fingerprint("a.py", None, original_body)
    p = _gh_provider([f"earlier wording\n\n<!-- pr-agent-dedup-code: {code_fp} -->"])

    def _replace_with_diff(suggestions):
        transformed = suggestions[0].copy()
        transformed["body"] = "**Suggestion:** fix it\n<details>\n```diff\n-x = 0\n+x = 1\n```\n</details>"
        return [transformed]

    p.validate_comments_inside_hunks = MagicMock(side_effect=_replace_with_diff)
    gs = _patch_flag(True)
    try:
        assert p.publish_code_suggestions([{
            "body": original_body,
            "relevant_file": "a.py",
            "relevant_lines_start": 10,
            "relevant_lines_end": 10,
        }])
    finally:
        gs.stop()

    p.pr.create_review.assert_not_called()


def test_github_does_not_send_pre_transform_fingerprint_to_api():
    original_body = "**Suggestion:** fix it\n```suggestion\nx = 1\n```"
    p = _gh_provider([])

    def _replace_with_diff(suggestions):
        transformed = suggestions[0].copy()
        transformed["body"] = "**Suggestion:** fix it\n<details>\n```diff\n-x = 0\n+x = 1\n```\n</details>"
        return [transformed]

    p.validate_comments_inside_hunks = MagicMock(side_effect=_replace_with_diff)
    gs = _patch_flag(True)
    try:
        assert p.publish_code_suggestions([{
            "body": original_body,
            "relevant_file": "a.py",
            "relevant_lines_start": 10,
            "relevant_lines_end": 10,
        }])
    finally:
        gs.stop()

    published = p.pr.create_review.call_args.kwargs["comments"]
    assert "_dedup_code_fp" not in published[0]
    assert "<!-- pr-agent-dedup-code:" in published[0]["body"]


def test_github_strips_pre_transform_fingerprint_when_feature_is_disabled():
    p = _gh_provider([])
    p.validate_comments_inside_hunks = MagicMock(side_effect=lambda suggestions: suggestions)
    gs = _patch_flag(False)
    try:
        assert p.publish_code_suggestions([{
            "body": "**Suggestion:** fix it\n```suggestion\nx = 1\n```",
            "relevant_file": "a.py",
            "relevant_lines_start": 10,
            "relevant_lines_end": 10,
        }])
    finally:
        gs.stop()

    published = p.pr.create_review.call_args.kwargs["comments"]
    assert "_dedup_code_fp" not in published[0]
    assert "pr-agent-dedup" not in published[0]["body"]


def test_store_unsupported_provider_degrades():
    class FooProvider:
        pass
    store = d.InlineCommentStore(FooProvider())
    assert store.load() == set()
    assert store.seen("abcabcabcabc") is False


def test_gitlab_skips_when_existing_discussion_has_marker():
    body = "**Suggestion:** already here [possible issue, importance: 7]"
    seen_fp = d.body_fingerprint("a.py", 10, body)
    p = _gl_provider([f"already here\n\n<!-- pr-agent-dedup: {seen_fp} -->"])
    gs = patch("pr_agent.git_providers.gitlab_provider.get_settings")
    m = gs.start()
    m.return_value.get.side_effect = _flag_side_effect(True)
    try:
        _send(p, body)
        p.mr.discussions.create.assert_not_called()
    finally:
        gs.stop()


def test_gitlab_fallback_note_carries_marker_and_records():
    p = _gl_provider([])
    p.mr.discussions.create.side_effect = RuntimeError("position rejected")
    p.get_line_link = MagicMock(return_value="http://link")
    original = {
        "relevant_lines_start": 10, "relevant_lines_end": 11,
        "existing_code": "a = 1", "improved_code": "a = 2",
        "suggestion_content": "fix it", "label": "possible issue", "score": 7,
    }

    def _send_fb():
        p.send_inline_comment(
            body="**Suggestion:** fix it [possible issue, importance: 7]",
            edit_type="addition", found=True, relevant_file="a.py",
            relevant_line_in_file="+a = 2", source_line_no=10,
            target_file=_FakeTargetFile(), target_line_no=10,
            original_suggestion=original,
        )

    gs = patch("pr_agent.git_providers.gitlab_provider.get_settings")
    m = gs.start()
    m.return_value.get.side_effect = _flag_side_effect(True)
    try:
        _send_fb()
        assert p.mr.notes.create.called
        note_body = p.mr.notes.create.call_args.args[0]["body"]
        assert "<!-- pr-agent-dedup:" in note_body
        # second identical send is skipped because the fallback recorded the fp
        _send_fb()
        assert p.mr.notes.create.call_count == 1
    finally:
        gs.stop()


def test_gitlab_skips_when_fallback_note_has_marker():
    # a prior run posted via the general-note fallback (mr.notes.create); its
    # marker must be found by scanning notes, not only discussions.
    body = "**Suggestion:** from fallback [possible issue, importance: 7]"
    seen_fp = d.body_fingerprint("a.py", 10, body)
    p = _gl_provider([])
    note = MagicMock()
    note.body = f"from fallback\n\n<!-- pr-agent-dedup: {seen_fp} -->"
    p.mr.notes.list.return_value = [note]
    gs = patch("pr_agent.git_providers.gitlab_provider.get_settings")
    m = gs.start()
    m.return_value.get.side_effect = _flag_side_effect(True)
    try:
        _send(p, body)
        p.mr.discussions.create.assert_not_called()
    finally:
        gs.stop()


def _flag_on_gitlab():
    gs = patch("pr_agent.git_providers.gitlab_provider.get_settings")
    m = gs.start()
    m.return_value.get.side_effect = _flag_side_effect(True)
    return gs


def test_gitlab_deletion_anchored_on_source_line():
    # deletions anchor on the old (source) line; two deletions sharing a
    # target line but different source lines must not collide.
    p = _gl_provider([])
    gs = _flag_on_gitlab()
    try:
        _send(p, "**Suggestion:** drop it [possible issue, importance: 7]", edit_type="deletion")
        first = p.mr.discussions.create.call_args.args[0]
        expected = d.body_fingerprint(
            "a.py", 10,
            "**Suggestion:** drop it [possible issue, importance: 7]")
        assert f"<!-- pr-agent-dedup: {expected} -->" in first["body"]
    finally:
        gs.stop()


def test_gitlab_marker_survives_when_body_clipped():
    p = _gl_provider([])
    p.max_comment_chars = 60
    gs = _flag_on_gitlab()
    try:
        _send(p, "x" * 500)
        posted = p.mr.discussions.create.call_args.args[0]["body"]
        assert "<!-- pr-agent-dedup:" in posted
        assert len(posted) <= 60
    finally:
        gs.stop()


def test_github_fallback_republish_marks_and_does_not_filter():
    # A fallback re-publish (disable_fallback=True) of an unmarked, "fixed"
    # comment must still receive a marker and must not be filtered out, even if
    # its fingerprint is already in the store, so it dedups on later runs.
    p = _gh_provider([])
    gs = _patch_flag(True)
    try:
        store = d.get_inline_comment_store(p)
        body = "fixed comment with no code block"
        store.add(d.body_fingerprint("a.py", None, body))  # pretend already seen
        p.publish_inline_comments(
            [{"path": "a.py", "line": 10, "body": body}], disable_fallback=True)
    finally:
        gs.stop()
    published = p.pr.create_review.call_args.kwargs["comments"]
    assert len(published) == 1
    assert "<!-- pr-agent-dedup:" in published[0]["body"]


def test_code_fingerprint_is_case_sensitive():
    fp_lower = d.code_fingerprint("f.py", 1, "x\n```suggestion\nuserId = 1\n```")
    fp_upper = d.code_fingerprint("f.py", 1, "x\n```suggestion\nUSERID = 1\n```")
    assert fp_lower != fp_upper


def test_fingerprints_are_marker_invariant():
    plain = "**Suggestion:** fix it\n```suggestion\na = 1\n```"
    marked = plain + "\n\n" + d.build_markers(
        d.body_fingerprint("f.py", 1, plain), d.code_fingerprint("f.py", 1, plain))
    assert d.body_fingerprint("f.py", 1, plain) == d.body_fingerprint("f.py", 1, marked)
    assert d.code_fingerprint("f.py", 1, plain) == d.code_fingerprint("f.py", 1, marked)


def test_has_marker_requires_wellformed_marker():
    assert d.has_marker("body\n\n<!-- pr-agent-dedup: a1b2c3d4e5f6 -->")
    assert not d.has_marker("a comment that merely mentions <!-- pr-agent-dedup: in prose")
    assert not d.has_marker("no marker at all")
