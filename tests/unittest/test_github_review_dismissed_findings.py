import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from pr_agent.algo.inline_comment_dedup import key_issue_body_with_markers
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.tools.pr_reviewer import PRReviewer

_BODY = "**Possible Issue**\n\nThe lock is never released."


def _comment(node_id, body, login="pr-agent[bot]", **fields):
    return SimpleNamespace(node_id=node_id, body=body, user=SimpleNamespace(login=login),
                           path="app.py", line=2, start_line=None, original_line=2,
                           original_start_line=None, in_reply_to_id=None, **fields)


def _thread(node_id, resolved=True, resolver="alice"):
    return {"id": f"thread-{node_id}", "isResolved": resolved,
            "resolvedBy": {"login": resolver} if resolver is not None else None,
            "comments": {"nodes": [{"id": node_id}, {"id": f"{node_id}-reply"}]}}


def _provider(pages, comments):
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "owner/repo"
    provider.pr_num = 1
    provider.github_user_id = "pr-agent[bot]"
    provider.deployment_type = "user"
    provider.pr = MagicMock()
    provider.pr.get_comments.return_value = comments
    requester = MagicMock()
    responses = []
    for i, threads in enumerate(pages):
        payload = {"data": {"repository": {"pullRequest": {"reviewThreads": {
            "nodes": threads,
            "pageInfo": {"hasNextPage": i < len(pages) - 1, "endCursor": f"cursor-{i}"},
        }}}}}
        responses.append((200, {}, json.dumps(payload)))
    requester.graphql_query.side_effect = [({}, json.loads(response[2])) for response in responses]
    provider.github_client = SimpleNamespace(_Github__requester=requester)
    return provider, requester


def _dismissed(provider):
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = provider
    return reviewer._load_dismissed_key_issues()


def test_github_human_resolved_key_issue_is_dismissed_across_pages():
    body = key_issue_body_with_markers(_BODY, "aabbccddeeff", "112233445566")
    root = _comment("root", body)
    reply = _comment("root-reply", "By design: caller releases it.", login="alice")
    provider, requester = _provider([[_thread("other", resolver=None)], [_thread("root")]], [root, reply])

    assert _dismissed(provider) == [{"path": "app.py", "body": _BODY,
                                    "line_start": 2, "line_end": 2,
                                    "reply": "By design: caller releases it."}]
    assert requester.graphql_query.call_count == 2
    queries = [call.args[0] for call in requester.graphql_query.call_args_list]
    assert all("resolvedBy { login }" in query for query in queries)
    assert requester.graphql_query.call_args_list[1].args[1]["cursor"] == "cursor-0"
    assert list(provider._iter_code_suggestion_threads()) == []


def test_github_bot_resolved_unattributed_open_and_non_key_issue_threads_stay_ordinary():
    body = key_issue_body_with_markers(_BODY, "aabbccddeeff", "112233445566")
    cases = [(_thread("bot", resolver="PR-Agent[bot]"), _comment("bot", body)),
             (_thread("missing", resolver=None), _comment("missing", body)),
             (_thread("open", resolved=False), _comment("open", body)),
             (_thread("human-root"), _comment("human-root", body, login="alice")),
             (_thread("suggestion"), _comment("suggestion", "**Suggestion:** Rename this"))]
    provider, _ = _provider([[thread for thread, _ in cases]], [comment for _, comment in cases])

    assert _dismissed(provider) == []


def test_github_missing_location_and_unverifiable_identity_add_no_dismissal():
    body = key_issue_body_with_markers(_BODY, "aabbccddeeff", "112233445566")
    root = _comment("root", body)
    root.line = None
    root.original_line = None
    provider, _ = _provider([[_thread("root")]], [root])
    assert _dismissed(provider) == []
    root.line = 2
    provider.github_user_id = ""
    provider._agent_login = lambda: ""
    assert _dismissed(provider) == []
