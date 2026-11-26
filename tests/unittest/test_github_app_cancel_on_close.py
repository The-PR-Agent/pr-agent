"""A pull_request "closed" delivery cancels the work still running for that PR in the GitHub App server."""

import asyncio

import pytest

from pr_agent.servers import github_app

PR = "https://api.github.com/repos/o/r/pulls/1"
OTHER = "https://api.github.com/repos/o/r/pulls/2"


def pr_event(action, url=PR):
    return {"action": action, "pull_request": {"url": url}}


class Dispatch:
    """Stands in for _dispatch_request: "opened" and "created" run until released; records how each one ended."""

    def __init__(self):
        self.ended, self.started, self.release = [], asyncio.Event(), asyncio.Event()

    async def __call__(self, body, event, action):
        try:
            if action in ("opened", "created"):
                self.started.set()
                await self.release.wait()
            self.ended.append((event, action, "finished"))
        except asyncio.CancelledError:
            self.ended.append((event, action, "cancelled"))
            raise


@pytest.fixture
def dispatch(monkeypatch):
    fake = Dispatch()
    monkeypatch.setattr(github_app, "_dispatch_request", fake)
    monkeypatch.setattr(github_app, "_pr_tasks", {})
    monkeypatch.setattr(github_app, "_closed_pr_tasks", set())
    return fake


async def test_closing_a_pr_cancels_its_running_review(dispatch):
    review = asyncio.create_task(github_app.handle_request(pr_event("opened"), "pull_request"))
    await dispatch.started.wait()
    await github_app.handle_request(pr_event("closed"), "pull_request")
    assert await review == {}
    assert dispatch.ended == [("pull_request", "opened", "cancelled"), ("pull_request", "closed", "finished")]
    assert github_app._pr_tasks == {}


async def test_closing_a_pr_leaves_other_prs_running(dispatch):
    other = asyncio.create_task(github_app.handle_request(pr_event("opened", OTHER), "pull_request"))
    await dispatch.started.wait()
    await github_app.handle_request(pr_event("closed"), "pull_request")
    dispatch.release.set()
    await other
    assert ("pull_request", "opened", "finished") in dispatch.ended


async def test_a_command_from_a_pr_comment_is_cancelled_too(dispatch):
    comment = {"action": "created", "issue": {"pull_request": {"url": PR}}, "comment": {"body": "/review"}}
    running = asyncio.create_task(github_app.handle_request(comment, "issue_comment"))
    await dispatch.started.wait()
    await github_app.handle_request(pr_event("closed"), "pull_request")
    assert await running == {}
    assert ("issue_comment", "created", "cancelled") in dispatch.ended


async def test_shutdown_cancellation_still_propagates(dispatch):
    review = asyncio.create_task(github_app.handle_request(pr_event("opened"), "pull_request"))
    await dispatch.started.wait()
    review.cancel()
    with pytest.raises(asyncio.CancelledError):
        await review
    assert github_app._pr_tasks == {}


async def test_work_cancelled_for_another_reason_still_raises(dispatch, monkeypatch):
    async def cancelled(body, event, action):
        raise asyncio.CancelledError()

    monkeypatch.setattr(github_app, "_dispatch_request", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await github_app.handle_request(pr_event("opened"), "pull_request")
    assert github_app._pr_tasks == {}


async def test_a_delivery_without_a_pr_is_not_registered(dispatch):
    await github_app.handle_request({"action": "deleted", "installation": {}}, "installation")
    assert dispatch.ended == [("installation", "deleted", "finished")] and github_app._pr_tasks == {}


async def test_a_second_close_does_not_interrupt_the_cancelled_work_s_cleanup(monkeypatch, dispatch):
    started, cleaning, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    ended = []

    async def slow_cleanup(body, event, action):
        if action != "opened":
            return
        try:
            started.set()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            try:
                await cleaned.wait()  # stands in for an async context manager releasing its slot
                ended.append("cleaned up")
            except asyncio.CancelledError:
                ended.append("cleanup interrupted")
                raise
            raise

    monkeypatch.setattr(github_app, "_dispatch_request", slow_cleanup)
    review = asyncio.create_task(github_app.handle_request(pr_event("opened"), "pull_request"))
    await started.wait()
    await github_app.handle_request(pr_event("closed"), "pull_request")
    await cleaning.wait()
    await github_app.handle_request(pr_event("closed"), "pull_request")  # GitHub redelivers, or the PR closes again
    cleaned.set()
    assert await review == {}
    assert ended == ["cleaned up"]


async def test_a_close_does_not_claim_work_that_already_ended_cancelled(monkeypatch, dispatch):
    closing = []

    async def cancelled_by_itself(body, event, action):
        if action == "opened":
            # The close is handled after the work has ended but before its handler reads why it ended.
            closing.append(asyncio.create_task(github_app.handle_request(pr_event("closed"), "pull_request")))
            raise asyncio.CancelledError()

    monkeypatch.setattr(github_app, "_dispatch_request", cancelled_by_itself)
    with pytest.raises(asyncio.CancelledError):
        await github_app.handle_request(pr_event("opened"), "pull_request")
    await closing[0]
    assert github_app._pr_tasks == {} and github_app._closed_pr_tasks == set()
