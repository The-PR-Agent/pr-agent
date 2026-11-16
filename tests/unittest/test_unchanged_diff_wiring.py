from unittest.mock import MagicMock, patch

import pytest

from pr_agent.algo.run_details import init_run_details, record_command_failure
from pr_agent.commands.pr_description import PRDescription
from pr_agent.commands.pr_reviewer import PRReviewer
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import IncrementalPR


class FakeGuard:
    instances = []

    def __init__(self, tool, git_provider, skip=False):
        self.tool = tool
        self.skip = skip
        self.recorded = False
        FakeGuard.instances.append(self)

    def should_skip(self):
        return self.skip

    def record(self):
        self.recorded = True


@pytest.fixture
def guards(monkeypatch):
    FakeGuard.instances = []
    monkeypatch.setitem(get_settings().pr_reviewer, "per_commit_review", False)
    monkeypatch.setitem(get_settings().config, "skip_unchanged_diff", True)
    return FakeGuard.instances


def _guard_factory(skip):
    return lambda tool, provider: FakeGuard(tool, provider, skip=skip)


def _reviewer(incremental=False):
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = MagicMock()
    reviewer.pr_url = "https://example/pr/1"
    reviewer.incremental = IncrementalPR(incremental)
    return reviewer


def _review(reviewer, done=True, fail=False):
    async def run_review():
        init_run_details()
        reviewer._review_done = done
        if fail:
            record_command_failure()
    return run_review


@pytest.mark.parametrize("skip", [True, False])
async def test_review_runs_only_when_not_skipped(guards, skip):
    reviewer = _reviewer()
    run_review = MagicMock(side_effect=_review(reviewer))
    with patch("pr_agent.commands.pr_reviewer.UnchangedDiffGuard", _guard_factory(skip)), \
            patch.object(reviewer, "_run_review", side_effect=run_review):
        await reviewer.run()

    assert run_review.called is not skip
    assert guards[0].tool == "review"


@pytest.mark.parametrize("done, fail, recorded", [(True, False, True), (False, False, False), (True, True, False)])
async def test_review_is_recorded_only_when_it_succeeded(guards, done, fail, recorded):
    reviewer = _reviewer()
    with patch("pr_agent.commands.pr_reviewer.UnchangedDiffGuard", _guard_factory(False)), \
            patch.object(reviewer, "_run_review", side_effect=_review(reviewer, done, fail)):
        await reviewer.run()

    assert guards[0].recorded is recorded


async def test_incremental_review_is_never_skipped_or_recorded(guards):
    reviewer = _reviewer(incremental=True)
    with patch("pr_agent.commands.pr_reviewer.UnchangedDiffGuard", _guard_factory(True)), \
            patch.object(reviewer, "_run_review", side_effect=_review(reviewer)) as run_review:
        await reviewer.run()

    assert run_review.called
    assert guards == []


def _describer():
    describer = PRDescription.__new__(PRDescription)
    describer.git_provider = MagicMock()
    return describer


@pytest.mark.parametrize("skip", [True, False])
async def test_describe_runs_only_when_not_skipped(guards, skip):
    describer = _describer()
    with patch("pr_agent.commands.pr_description.UnchangedDiffGuard", _guard_factory(skip)), \
            patch.object(describer, "_run_describe", return_value="") as run_describe:
        await describer.run()

    assert run_describe.called is not skip
    assert guards[0].tool == "describe"


@pytest.mark.parametrize("result, fail, recorded", [("", False, True), (None, False, False), ("", True, False)])
async def test_describe_is_recorded_only_when_it_succeeded(guards, result, fail, recorded):
    describer = _describer()

    async def run_describe():
        init_run_details()
        if fail:
            record_command_failure()
        return result

    with patch("pr_agent.commands.pr_description.UnchangedDiffGuard", _guard_factory(False)), \
            patch.object(describer, "_run_describe", side_effect=run_describe):
        assert await describer.run() == result

    assert guards[0].recorded is recorded


async def test_review_builds_no_guard_when_the_setting_is_off(guards, monkeypatch):
    monkeypatch.setitem(get_settings().config, "skip_unchanged_diff", False)
    reviewer = _reviewer()
    with patch("pr_agent.commands.pr_reviewer.UnchangedDiffGuard", _guard_factory(True)), \
            patch.object(reviewer, "_run_review", side_effect=_review(reviewer)) as run_review:
        await reviewer.run()

    assert run_review.called
    assert guards == []
