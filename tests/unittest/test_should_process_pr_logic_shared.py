"""Every webhook server must apply the same "should this PR be processed" policy.

The five servers extract different payload shapes, so each test drives all of them with an
equivalent pull request and asserts they agree. This is what keeps the shared policy in
`pr_agent/servers/utils.py` from drifting back into five copies.
"""

import copy
import importlib

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.servers import bitbucket_app, bitbucket_server_webhook, gitea_app, github_app
from pr_agent.servers.utils import should_process_pr_logic as shared_should_process_pr_logic
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

IGNORE_KEYS = (
    "CONFIG.IGNORE_REPOSITORIES",
    "CONFIG.IGNORE_PR_AUTHORS",
    "CONFIG.IGNORE_PR_TITLE",
    "CONFIG.IGNORE_PR_LABELS",
    "CONFIG.IGNORE_PR_SOURCE_BRANCHES",
    "CONFIG.IGNORE_PR_TARGET_BRANCHES",
)

DEFAULTS = {
    "title": "Regular PR",
    "sender": "alice",
    "source_branch": "feature/cache",
    "target_branch": "main",
    "repo_full_name": "org/repo",
    "labels": (),
}


def _fields(**overrides):
    fields = dict(DEFAULTS)
    fields.update(overrides)
    return fields


def _github_body(fields):
    return {
        "pull_request": {
            "title": fields["title"],
            "labels": [{"name": label} for label in fields["labels"]],
            "head": {"ref": fields["source_branch"]},
            "base": {"ref": fields["target_branch"]},
        },
        "sender": {"login": fields["sender"]},
        "repository": {"full_name": fields["repo_full_name"]},
    }


def _bitbucket_payload(fields):
    return {
        "data": {
            "pullrequest": {
                "title": fields["title"],
                "source": {"branch": {"name": fields["source_branch"]}},
                "destination": {
                    "branch": {"name": fields["target_branch"]},
                    "repository": {"full_name": fields["repo_full_name"]},
                },
            },
            "actor": {"username": fields["sender"]},
        }
    }


def _bitbucket_server_payload(fields):
    project_key, _, repo_slug = fields["repo_full_name"].partition("/")
    return {
        "pullRequest": {
            "id": 7,
            "title": fields["title"],
            "fromRef": {"displayId": fields["source_branch"]},
            "toRef": {
                "displayId": fields["target_branch"],
                "repository": {"slug": repo_slug, "project": {"key": project_key}},
            },
            "author": {"user": {"name": fields["sender"]}},
        }
    }


def _gitlab_payload(fields):
    return {
        "object_attributes": {
            "title": fields["title"],
            "source_branch": fields["source_branch"],
            "target_branch": fields["target_branch"],
            "labels": [{"title": label} for label in fields["labels"]],
        },
        "project": {"path_with_namespace": fields["repo_full_name"]},
        "user": {"username": fields["sender"]},
    }


@pytest.fixture(scope="module")
def gitlab_webhook_module():
    settings = get_settings()
    original_git_provider = settings.config.get("git_provider", None)
    had_gitlab_settings = "GITLAB" in settings
    original_gitlab_settings = copy.deepcopy(settings.get("GITLAB", None))
    settings.set("GITLAB.URL", "https://gitlab.com")
    try:
        yield importlib.import_module("pr_agent.servers.gitlab_webhook")
    finally:
        settings.config.git_provider = original_git_provider
        if had_gitlab_settings:
            settings.set("GITLAB", original_gitlab_settings)
        else:
            settings.unset("GITLAB", force=True)


@pytest.fixture
def providers(gitlab_webhook_module):
    """(name, should_process_pr_logic, payload builder, carries labels) for every server."""
    return (
        ("github", github_app.should_process_pr_logic, _github_body, True),
        ("gitea", gitea_app.should_process_pr_logic, _github_body, True),
        ("gitlab", gitlab_webhook_module.should_process_pr_logic, _gitlab_payload, True),
        ("bitbucket", bitbucket_app.should_process_pr_logic, _bitbucket_payload, False),
        ("bitbucket_server", bitbucket_server_webhook.should_process_pr_logic, _bitbucket_server_payload, False),
    )


@pytest.fixture
def ignore_settings():
    snapshot = snapshot_settings(IGNORE_KEYS)
    settings = get_settings()
    for key in IGNORE_KEYS:
        settings.set(key, [])
    yield settings
    restore_settings(snapshot)


def _assert_all(providers, fields, expected, reason, only_with_labels=False):
    for name, should_process, build_payload, carries_labels in providers:
        if only_with_labels and not carries_labels:
            continue
        result = should_process(build_payload(fields))
        assert result is expected, f"{name}: expected {expected} for {reason}, got {result}"


def test_unfiltered_pr_is_processed_by_every_server(providers, ignore_settings):
    _assert_all(providers, _fields(), True, "a PR that matches no ignore rule")


@pytest.mark.parametrize(
    "setting, patterns, matching, non_matching",
    [
        ("CONFIG.IGNORE_REPOSITORIES", ["^org/repo$"], {"repo_full_name": "org/repo"},
         {"repo_full_name": "org/other"}),
        ("CONFIG.IGNORE_PR_AUTHORS", ["^dependabot"], {"sender": "dependabot[bot]"}, {"sender": "alice"}),
        ("CONFIG.IGNORE_PR_TITLE", ["^WIP"], {"title": "WIP: cache"}, {"title": "Add cache"}),
        ("CONFIG.IGNORE_PR_SOURCE_BRANCHES", ["^generated/"], {"source_branch": "generated/api"},
         {"source_branch": "feature/api"}),
        ("CONFIG.IGNORE_PR_TARGET_BRANCHES", ["^legacy$"], {"target_branch": "legacy"}, {"target_branch": "main"}),
    ],
)
def test_every_server_applies_the_same_ignore_rule(providers, ignore_settings, setting, patterns, matching,
                                                   non_matching):
    ignore_settings.set(setting, patterns)
    _assert_all(providers, _fields(**matching), False, f"{setting}={patterns} matching {matching}")
    _assert_all(providers, _fields(**non_matching), True, f"{setting}={patterns} not matching {non_matching}")


def test_label_rule_is_shared_by_the_servers_that_carry_labels(providers, ignore_settings):
    ignore_settings.set("CONFIG.IGNORE_PR_LABELS", ["skip-pr-agent"])
    _assert_all(providers, _fields(labels=("skip-pr-agent",)), False, "an ignored label", only_with_labels=True)
    _assert_all(providers, _fields(labels=("ready",)), True, "a label that is not ignored", only_with_labels=True)


def test_ignore_pr_title_given_as_a_plain_string_is_not_iterated_per_character(providers, ignore_settings):
    """A string setting must be treated as one pattern, not as a sequence of characters.

    Iterating "^WIP" yields "^", which matches every title, so an unrelated PR would be
    dropped. Dynaconf hands the value through unchanged when it is written as a string.
    """
    ignore_settings.set("CONFIG.IGNORE_PR_TITLE", "^WIP")
    _assert_all(providers, _fields(title="WIP: cache"), False, "a string ignore_pr_title that matches")
    _assert_all(providers, _fields(title="Add cache"), True, "a string ignore_pr_title that does not match")


def test_an_invalid_pattern_does_not_filter_out_every_pull_request(providers, ignore_settings):
    """On failure the PR is processed; the opposite would silently disable the agent."""
    ignore_settings.set("CONFIG.IGNORE_PR_TITLE", ["("])
    _assert_all(providers, _fields(), True, "an unparsable ignore_pr_title regex")


def test_shared_helper_defaults_to_processing(ignore_settings):
    assert shared_should_process_pr_logic() is True
