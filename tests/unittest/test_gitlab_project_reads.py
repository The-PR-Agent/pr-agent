"""Pin how many API calls the GitLab provider's project and default-branch reads cost.

`id_project` is known before anything is fetched, and every path that wanted a branch name or a
blob used to spend a full project GET for it: `get_repo_file_content` runs once per repo-context
file, `get_repo_settings_contents` once per nested config, and `get_canonical_url_parts` once per
link. A project GET is one of the larger responses GitLab sends.

The client here is a real `gitlab.Gitlab` with only its transport replaced, so the counts are the
counts python-gitlab would make. A mock of `gl.projects` cannot tell a lazy handle from a fetched
one and would keep passing if the project fetches came back.
"""

import base64
import json
from types import SimpleNamespace

import gitlab
from gitlab import GitlabGetError

from pr_agent.git_providers.gitlab_provider import GitLabProvider

DEFAULT_BRANCH = "main"


class _Response:
    """The parts of `requests.Response` that python-gitlab reads."""

    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.headers = {"Content-Type": "application/json"}
        self.encoding = "utf-8"
        self.text = json.dumps(body)
        self.content = self.text.encode()
        self.reason = "OK"
        self.url = "https://example.invalid"
        self.links = {}

    def json(self):
        return self._body

    def raise_for_status(self):
        return None


def _provider(blobs=None, mr_target_branch="main", project_body=None, missing_refs=()):
    """A provider on a client that records requests instead of sending them."""
    calls = []
    blobs = blobs or {}
    missing_refs = set(missing_refs)

    def http_request(method, path, **kwargs):
        calls.append((method.upper(), path))
        if method == "get" and "/repository/tree" in path:
            # python-gitlab hands the branch over as query data, not in the path
            ref = (kwargs.get("query_data") or {}).get("ref", "")
            if ref in missing_refs:
                raise GitlabGetError(f"404 Tree Not Found for {ref}", response_code=404)
            return _Response(200, [{"path": "a/.pr_agent.toml", "type": "blob"}])
        if method == "get" and "/repository/files/" in path:
            name = path.split("/repository/files/")[1].split("/")[0]
            if name in blobs:
                encoded = base64.b64encode(blobs[name]).decode()
                return _Response(200, {"file_name": name, "encoding": "base64",
                                       "content": encoded, "ref": DEFAULT_BRANCH})
            # python-gitlab turns this status into GitlabGetError inside `http_request`, which is
            # the function this fake replaces, so raise it here instead of returning a 404 body.
            raise GitlabGetError("404 File Not Found", response_code=404)
        if method == "get":
            return _Response(200, project_body if project_body is not None
                             else {"id": 1, "path_with_namespace": "group/repo",
                                   "default_branch": DEFAULT_BRANCH,
                                   "web_url": "https://example.invalid/group/repo"})
        return _Response(204, None)

    client = gitlab.Gitlab("https://example.invalid", private_token="token")
    client.http_request = http_request

    provider = GitLabProvider.__new__(GitLabProvider)
    provider.gl = client
    provider.id_project = "group/repo"
    provider.id_mr = 7
    # the provider only ever reads `target_branch` off the MR
    provider.mr = SimpleNamespace(id=7, iid=7, target_branch=mr_target_branch)
    provider.pr_url = "https://example.invalid/group/repo/-/merge_requests/7"
    return provider, calls


def _project_gets(calls):
    return [c for c in calls
            if c[0] == "GET" and c[1].rstrip("/").endswith("projects/group%2Frepo")]


def _tree_gets(calls):
    return [c for c in calls if c[0] == "GET" and "/repository/tree" in c[1]]


def _settings_off(monkeypatch):
    """Turn off the namespace-wide settings file so the local paths are the only ones exercised."""
    monkeypatch.setattr(GitLabProvider, "_get_global_repo_settings", lambda self: "")
    monkeypatch.setattr(GitLabProvider, "get_owning_namespace", lambda self: "group")


class TestRepoSettingsReadsAreLazy:
    """The two per-run config lookups, which is where an eager project GET actually cost."""

    def test_loading_the_local_config_reads_the_project_once_for_the_branch_name(self, monkeypatch):
        """Resolving the default branch costs one project GET; the payload is never downloaded."""
        _settings_off(monkeypatch)
        monkeypatch.setattr("pr_agent.git_providers.gitlab_provider.get_config_branch", lambda: None)
        provider, calls = _provider(blobs={".pr_agent.toml": b"[config]\n"})

        assert provider.get_repo_settings()
        assert provider.get_repo_settings()
        assert len(_project_gets(calls)) == 1, calls
        assert [c for c in calls if "repository/files" in c[1]], "the config file itself was read"

    def test_listing_the_config_tree_reads_the_project_once_for_the_branch_name(self):
        provider, calls = _provider()

        assert provider.get_repo_settings_tree("")[1] == DEFAULT_BRANCH
        assert provider.get_repo_settings_tree("")[1] == DEFAULT_BRANCH
        assert len(_project_gets(calls)) == 1, calls
        assert any("repository/tree" in c[1] for c in calls), "the tree itself was read"

    def test_a_missing_tree_for_the_asked_branch_falls_back_to_the_default_branch(self):
        """The retry reads the branch name from the cache: a lazy handle has no `default_branch`."""
        provider, calls = _provider(missing_refs={"stale-branch"})

        paths, ref = provider.get_repo_settings_tree("stale-branch")

        assert ref == DEFAULT_BRANCH
        assert paths == ["a/.pr_agent.toml"]
        assert len(_project_gets(calls)) == 1, calls

    def test_a_missing_tree_on_the_default_branch_too_is_not_an_error(self):
        """Asking for "" already resolved to the default branch, so the retry must not repeat it."""
        provider, calls = _provider(missing_refs={DEFAULT_BRANCH})

        assert provider.get_repo_settings_tree("") == ([], "")
        assert len(_tree_gets(calls)) == 1, "one tree read, not the same ref twice"

    def test_a_missing_tree_on_both_the_asked_branch_and_the_default_is_not_an_error(self):
        """The retry can fail too; nested settings are then skipped, not raised."""
        provider, calls = _provider(missing_refs={"stale-branch", DEFAULT_BRANCH})

        assert provider.get_repo_settings_tree("stale-branch") == ([], "")
        assert len([c for c in calls if "/repository/tree" in c[1]]) == 2, "both trees were tried"


class TestDefaultBranchIsReadOnce:
    def test_repeated_reads_of_the_default_branch_cost_one_project_get(self):
        provider, calls = _provider()

        first = provider._project_default_branch()
        second = provider._project_default_branch()

        assert first == DEFAULT_BRANCH
        assert second == DEFAULT_BRANCH
        assert len(_project_gets(calls)) == 1, calls

    def test_a_project_with_no_default_branch_is_asked_once(self):
        """An empty repository reports null, and that answer is worth caching like any other."""
        provider, calls = _provider(project_body={"id": 1, "default_branch": None})

        assert provider._project_default_branch() is None
        assert provider._project_default_branch() is None
        assert len(_project_gets(calls)) == 1, calls

    def test_the_repo_context_ref_shares_that_read(self):
        """It used to keep a second cache of the same fact, so the two could each pay a GET."""
        provider, calls = _provider()

        assert provider.get_repo_context_ref(from_default_branch=True) == DEFAULT_BRANCH
        assert provider.get_repo_context_ref(from_default_branch=True) == DEFAULT_BRANCH
        assert provider._project_default_branch() == DEFAULT_BRANCH
        assert len(_project_gets(calls)) == 1, calls

    def test_the_mr_target_branch_still_wins_for_repo_context(self):
        provider, calls = _provider(mr_target_branch="release/2.0")

        assert provider.get_repo_context_ref() == "release/2.0"
        assert _project_gets(calls) == [], "the MR target needs no project read at all"


class TestProjectReadsAreLazy:
    """These paths need a manager, not a project payload, so they should not download one."""

    def test_reading_a_repo_file_does_not_fetch_the_project(self):
        provider, calls = _provider(blobs={"src%2Fapp.py": b"print(1)\n"})

        assert provider.get_repo_file_content("src/app.py") == "print(1)\n"
        assert _project_gets(calls) == [], calls

    def test_reading_a_repo_file_from_the_default_branch_reuses_the_cached_name(self):
        provider, calls = _provider(blobs={"AGENTS.md": b"hi\n"})

        assert provider.get_repo_file_content("AGENTS.md") == "hi\n"
        assert provider.get_repo_file_content("AGENTS.md", from_default_branch=True) == "hi\n"
        assert len(_project_gets(calls)) == 1, "one project read, for the default branch name"

    def test_reading_many_nested_configs_does_not_fetch_the_project(self):
        contents = {name: f"[config]\n# {name}\n".encode() for name in
                    ("a/.pr_agent.toml", "b/.pr_agent.toml", "c/.pr_agent.toml")}
        provider, calls = _provider(blobs={name.replace("/", "%2F"): body
                                            for name, body in contents.items()})

        result = provider.get_repo_settings_contents(list(contents), DEFAULT_BRANCH)

        assert _project_gets(calls) == [], calls
        # the reads themselves have to happen: an implementation that skipped them and returned
        # {} would satisfy the assertion above
        assert result == contents, result

    def test_a_missing_file_is_still_an_empty_string_not_an_error(self):
        provider, _ = _provider(blobs={})

        assert provider.get_repo_file_content("nope.md") == ""

    def test_canonical_url_parts_uses_the_cached_default_branch(self):
        provider, calls = _provider()

        prefix, _ = provider.get_canonical_url_parts()

        assert prefix == f"https://example.invalid/group/repo/-/blob/{DEFAULT_BRANCH}", prefix
        assert len(_project_gets(calls)) == 1, calls
        # a second link must not pay for the name again
        before = len(calls)
        provider.get_canonical_url_parts()
        assert len(_project_gets(calls[before:])) == 0, calls
