from types import SimpleNamespace

from pr_agent.git_providers.github_provider import GithubProvider


class _FakeRequester:
    def __init__(self):
        self.calls = []
        self.responses = {}

    def set_response(self, method, url, response_data):
        self.responses[(method, url)] = response_data

    def set_exception(self, method, url, exception):
        self.responses[(method, url)] = exception

    def requestJsonAndCheck(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        key = (method, url)
        if key in self.responses:
            result = self.responses[key]
            if isinstance(result, Exception):
                raise result
            return result
        return {}, {"id": 99}


def _make_provider(last_commit_sha="deadbeef", requester=None):
    p = GithubProvider.__new__(GithubProvider)
    p.repo = "owner/repo"
    p.base_url = "https://api.github.com"
    p.pr = SimpleNamespace(_requester=requester or _FakeRequester())
    p.last_commit_id = SimpleNamespace(sha=last_commit_sha) if last_commit_sha else None
    p._check_run_ids = {}
    return p


# ---------------------------------------------------------------------------
# _publish_check_run - create path
# ---------------------------------------------------------------------------

def test_publish_check_run_creates_when_no_existing():
    requester = _FakeRequester()
    provider = _make_provider(requester=requester)
    requester.set_response(
        "GET",
        f"{provider.base_url}/repos/{provider.repo}/commits/deadbeef/check-runs",
        ({}, {"check_runs": []}),
    )
    requester.set_response(
        "POST",
        f"{provider.base_url}/repos/{provider.repo}/check-runs",
        ({}, {"id": 101}),
    )

    result = provider._publish_check_run("some output", "review")

    assert result is True
    assert provider._check_run_ids.get("review") == 101


def test_publish_check_run_returns_false_on_post_failure():
    requester = _FakeRequester()
    provider = _make_provider(requester=requester)
    requester.set_response(
        "GET",
        f"{provider.base_url}/repos/{provider.repo}/commits/deadbeef/check-runs",
        ({}, {"check_runs": []}),
    )
    requester.set_exception(
        "POST",
        f"{provider.base_url}/repos/{provider.repo}/check-runs",
        RuntimeError("API error"),
    )

    result = provider._publish_check_run("some output", "review")

    assert result is False
    assert "review" not in provider._check_run_ids


def test_publish_check_run_returns_false_when_no_commit_sha():
    provider = _make_provider(last_commit_sha=None)
    result = provider._publish_check_run("some output", "review")
    assert result is False


def test_publish_check_run_truncates_long_text():
    requester = _FakeRequester()
    provider = _make_provider(requester=requester)
    requester.set_response(
        "GET",
        f"{provider.base_url}/repos/{provider.repo}/commits/deadbeef/check-runs",
        ({}, {"check_runs": []}),
    )
    requester.set_response(
        "POST",
        f"{provider.base_url}/repos/{provider.repo}/check-runs",
        ({}, {"id": 102}),
    )

    long_text = "A" * 70000
    result = provider._publish_check_run(long_text, "review")

    assert result is True
    post_call = [c for c in requester.calls if c[0] == "POST"][0]
    body = post_call[2]["input"]
    assert len(body["output"]["text"]) == 65535


# ---------------------------------------------------------------------------
# _publish_check_run - update path
# ---------------------------------------------------------------------------

def test_publish_check_run_updates_when_existing_found_via_api():
    requester = _FakeRequester()
    provider = _make_provider(requester=requester)
    requester.set_response(
        "GET",
        f"{provider.base_url}/repos/{provider.repo}/commits/deadbeef/check-runs",
        (
            {},
            {
                "check_runs": [
                    {"id": 42, "name": "PR Agent - Review"},
                ]
            },
        ),
    )
    requester.set_response(
        "PATCH",
        f"{provider.base_url}/repos/{provider.repo}/check-runs/42",
        ({}, {}),
    )

    result = provider._publish_check_run("updated output", "review")

    assert result is True
    assert provider._check_run_ids.get("review") == 42
    patch_call = [c for c in requester.calls if c[0] == "PATCH"]
    assert len(patch_call) == 1


def test_publish_check_run_updates_when_existing_cached():
    requester = _FakeRequester()
    provider = _make_provider(requester=requester)
    provider._check_run_ids["review"] = 42
    requester.set_response(
        "PATCH",
        f"{provider.base_url}/repos/{provider.repo}/check-runs/42",
        ({}, {}),
    )

    result = provider._publish_check_run("updated output", "review")

    assert result is True
    # No GET call was made since the ID was already cached
    get_calls = [c for c in requester.calls if c[0] == "GET"]
    assert len(get_calls) == 0


def test_publish_check_run_falls_back_to_create_on_patch_failure():
    requester = _FakeRequester()
    provider = _make_provider(requester=requester)
    provider._check_run_ids["review"] = 42
    requester.set_exception(
        "PATCH",
        f"{provider.base_url}/repos/{provider.repo}/check-runs/42",
        RuntimeError("Update failed"),
    )
    requester.set_response(
        "POST",
        f"{provider.base_url}/repos/{provider.repo}/check-runs",
        ({}, {"id": 103}),
    )

    result = provider._publish_check_run("output after update failed", "review")

    assert result is True
    assert provider._check_run_ids.get("review") == 103


# ---------------------------------------------------------------------------
# _find_existing_check_run
# ---------------------------------------------------------------------------

def test_find_existing_check_run_returns_id_on_match():
    requester = _FakeRequester()
    provider = _make_provider(requester=requester)
    requester.set_response(
        "GET",
        f"{provider.base_url}/repos/{provider.repo}/commits/deadbeef/check-runs",
        (
            {},
            {
                "check_runs": [
                    {"id": 1, "name": "PR Agent - Review"},
                    {"id": 2, "name": "PR Agent - Describe"},
                ]
            },
        ),
    )

    result = provider._find_existing_check_run("PR Agent - Review", "deadbeef")

    assert result == 1


def test_find_existing_check_run_returns_none_on_no_match():
    requester = _FakeRequester()
    provider = _make_provider(requester=requester)
    requester.set_response(
        "GET",
        f"{provider.base_url}/repos/{provider.repo}/commits/deadbeef/check-runs",
        (
            {},
            {
                "check_runs": [
                    {"id": 2, "name": "PR Agent - Describe"},
                ]
            },
        ),
    )

    result = provider._find_existing_check_run("PR Agent - Review", "deadbeef")

    assert result is None


def test_find_existing_check_run_returns_none_on_api_error():
    requester = _FakeRequester()
    provider = _make_provider(requester=requester)
    requester.set_exception(
        "GET",
        f"{provider.base_url}/repos/{provider.repo}/commits/deadbeef/check-runs",
        RuntimeError("API error"),
    )

    result = provider._find_existing_check_run("PR Agent - Review", "deadbeef")

    assert result is None
