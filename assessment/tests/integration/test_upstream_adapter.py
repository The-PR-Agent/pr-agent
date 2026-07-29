from __future__ import annotations

from types import SimpleNamespace

from assessment.ai_reviewer.diff_scope import parse_changed_scope
from assessment.ai_reviewer.upstream_adapter import UpstreamAdapter


class FakePullRequest:
    def __init__(self) -> None:
        self.head = SimpleNamespace(sha="b" * 40)
        self.created: list[dict] = []

    def get_review_comments(self):
        return (
            SimpleNamespace(
                body="bot body",
                user=SimpleNamespace(login="review-bot"),
            ),
            SimpleNamespace(
                body="human body",
                user=SimpleNamespace(login="developer"),
            ),
        )

    def create_review(self, **kwargs) -> None:
        self.created.append(kwargs)


class FakeProvider:
    def __init__(self) -> None:
        self.pr = FakePullRequest()
        self.github_client = SimpleNamespace(
            get_user=lambda: SimpleNamespace(login="review-bot")
        )
        self.commit = object()

    def get_files(self):
        return (
            SimpleNamespace(
                filename="src/added.py",
                previous_filename=None,
                status="added",
                patch="@@ -0,0 +1,1 @@\n+value = 1",
            ),
            SimpleNamespace(
                filename="src/new.py",
                previous_filename="src/old.py",
                status="renamed",
                patch="@@ -1,1 +1,1 @@\n-old = 1\n+new = 1",
            ),
            SimpleNamespace(
                filename="src/removed.py",
                previous_filename=None,
                status="removed",
                patch="@@ -1,1 +0,0 @@\n-old = 1",
            ),
        )

    def _get_repo(self):
        return SimpleNamespace(get_commit=lambda _: self.commit)


def test_adapter_builds_parseable_diff_for_file_statuses() -> None:
    adapter = UpstreamAdapter(FakeProvider())

    files, lines = parse_changed_scope(adapter.unified_diff())

    assert files == ("src/added.py", "src/new.py", "src/removed.py")
    assert lines["src/added.py"] == frozenset({1})
    assert lines["src/new.py"] == frozenset({1})
    assert lines["src/removed.py"] == frozenset()


def test_adapter_reads_only_bot_comments_and_creates_one_review() -> None:
    provider = FakeProvider()
    adapter = UpstreamAdapter(provider)
    comments = [{"path": "src/added.py", "line": 1, "body": "body"}]

    adapter.create_review("b" * 40, comments)

    assert adapter.current_head_sha() == "b" * 40
    assert adapter.existing_review_bodies() == ("bot body",)
    assert provider.pr.created == [
        {"commit": provider.commit, "comments": comments}
    ]
