from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .github_runtime import PullRequestEvent


class UpstreamAdapter:
    def __init__(self, provider: Any) -> None:
        self._provider = provider

    @classmethod
    def from_token(
        cls,
        event: PullRequestEvent,
        token: str,
    ) -> UpstreamAdapter:
        if not token:
            raise ValueError("GITHUB_TOKEN is required")
        from github import Github

        client = Github(login_or_token=token)
        repository = client.get_repo(event.repository)
        pull_request = repository.get_pull(event.pr_number)
        return cls(
            _GithubProviderView(
                github_client=client,
                repository=repository,
                pr=pull_request,
            )
        )

    def unified_diff(self) -> str:
        chunks: list[str] = []
        for changed_file in self._provider.get_files():
            path = str(changed_file.filename)
            previous = str(
                getattr(changed_file, "previous_filename", None) or path
            )
            status = str(changed_file.status)
            old_path = "/dev/null" if status == "added" else f"a/{previous}"
            new_path = "/dev/null" if status == "removed" else f"b/{path}"
            chunks.append(f"diff --git a/{previous} b/{path}\n")
            if status == "added":
                chunks.append("new file mode 100644\n")
            elif status == "removed":
                chunks.append("deleted file mode 100644\n")
            elif status == "renamed":
                chunks.extend(
                    [
                        f"rename from {previous}\n",
                        f"rename to {path}\n",
                    ]
                )
            chunks.extend(
                [f"--- {old_path}\n", f"+++ {new_path}\n"]
            )
            patch = getattr(changed_file, "patch", None)
            if patch:
                chunks.append(str(patch).rstrip("\n") + "\n")
        return "".join(chunks)

    def current_head_sha(self) -> str:
        return str(self._provider.pr.head.sha)

    def existing_review_bodies(self) -> tuple[str, ...]:
        actor = self._provider.github_client.get_user().login
        return tuple(
            str(comment.body or "")
            for comment in self._provider.pr.get_review_comments()
            if comment.user and comment.user.login == actor
        )

    def create_review(
        self,
        head_sha: str,
        comments: Iterable[dict[str, object]],
    ) -> None:
        commit = self._provider._get_repo().get_commit(head_sha)
        self._provider.pr.create_review(
            commit=commit,
            comments=list(comments),
        )


@dataclass(slots=True)
class _GithubProviderView:
    github_client: Any
    repository: Any
    pr: Any

    def get_files(self) -> Any:
        return self.pr.get_files()

    def _get_repo(self) -> Any:
        return self.repository
