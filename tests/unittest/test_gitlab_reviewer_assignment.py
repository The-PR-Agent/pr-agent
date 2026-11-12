import asyncio
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def setup_env(monkeypatch):
    monkeypatch.setenv("GITLAB__URL", "https://gitlab.example.com")


class TestIsBotAssignedAsReviewer:
    BOT_ID = 516

    @mock.patch("pr_agent.servers.gitlab_webhook._get_bot_user_id", new_callable=mock.AsyncMock)
    def test_detects_new_assignment(self, mock_bot_id):
        from pr_agent.servers.gitlab_webhook import is_bot_assigned_as_reviewer
        mock_bot_id.return_value = self.BOT_ID
        data = {
            "changes": {
                "reviewers": {
                    "previous": [],
                    "current": [{"id": self.BOT_ID, "username": "k2so-bot"}],
                }
            }
        }
        assert asyncio.run(is_bot_assigned_as_reviewer(data)) is True

    @mock.patch("pr_agent.servers.gitlab_webhook._get_bot_user_id", new_callable=mock.AsyncMock)
    def test_ignores_already_assigned(self, mock_bot_id):
        from pr_agent.servers.gitlab_webhook import is_bot_assigned_as_reviewer
        mock_bot_id.return_value = self.BOT_ID
        data = {
            "changes": {
                "reviewers": {
                    "previous": [{"id": self.BOT_ID, "username": "k2so-bot"}],
                    "current": [{"id": self.BOT_ID, "username": "k2so-bot"}],
                }
            }
        }
        assert asyncio.run(is_bot_assigned_as_reviewer(data)) is False

    @mock.patch("pr_agent.servers.gitlab_webhook._get_bot_user_id", new_callable=mock.AsyncMock)
    def test_no_reviewers_key(self, mock_bot_id):
        from pr_agent.servers.gitlab_webhook import is_bot_assigned_as_reviewer
        mock_bot_id.return_value = self.BOT_ID
        data = {"changes": {"updated_at": {"previous": "old", "current": "new"}}}
        assert asyncio.run(is_bot_assigned_as_reviewer(data)) is False

    def test_changes_not_dict(self):
        from pr_agent.servers.gitlab_webhook import is_bot_assigned_as_reviewer
        data = {"changes": "not-a-dict"}
        assert asyncio.run(is_bot_assigned_as_reviewer(data)) is False

    @mock.patch("pr_agent.servers.gitlab_webhook._get_bot_user_id", new_callable=mock.AsyncMock)
    def test_reviewers_not_dict(self, mock_bot_id):
        from pr_agent.servers.gitlab_webhook import is_bot_assigned_as_reviewer
        mock_bot_id.return_value = self.BOT_ID
        data = {"changes": {"reviewers": "not-a-dict"}}
        assert asyncio.run(is_bot_assigned_as_reviewer(data)) is False

    def test_no_changes_key(self):
        from pr_agent.servers.gitlab_webhook import is_bot_assigned_as_reviewer
        data = {"object_kind": "merge_request"}
        assert asyncio.run(is_bot_assigned_as_reviewer(data)) is False

    @mock.patch("pr_agent.servers.gitlab_webhook._get_bot_user_id", new_callable=mock.AsyncMock)
    def test_bot_id_unresolvable(self, mock_bot_id):
        from pr_agent.servers.gitlab_webhook import is_bot_assigned_as_reviewer
        mock_bot_id.return_value = None
        data = {
            "changes": {
                "reviewers": {
                    "previous": [],
                    "current": [{"id": self.BOT_ID}],
                }
            }
        }
        assert asyncio.run(is_bot_assigned_as_reviewer(data)) is False

    @mock.patch("pr_agent.servers.gitlab_webhook._get_bot_user_id", new_callable=mock.AsyncMock)
    def test_previous_with_non_dict_entries(self, mock_bot_id):
        from pr_agent.servers.gitlab_webhook import is_bot_assigned_as_reviewer
        mock_bot_id.return_value = self.BOT_ID
        data = {
            "changes": {
                "reviewers": {
                    "previous": [{"id": 100}, "not-a-dict"],
                    "current": [{"id": self.BOT_ID}],
                }
            }
        }
        assert asyncio.run(is_bot_assigned_as_reviewer(data)) is True


@pytest.mark.parametrize("reviewers, expected", [
    ([[], [{"id": 516}]], True),
    ([[{"id": 516, "re_requested": False}], [{"id": 516, "re_requested": True}]], True),
    ({"previous": [{"id": 516, "re_requested": False}],
      "current": [{"id": 516, "re_requested": True}]}, True),
    ([[{"id": 516, "re_requested": True}], [{"id": 516, "re_requested": True}]], False),
    ([[{"id": 516}], [{"id": 516, "re_requested": True}]], False),
    ([[{"id": 516, "state": "unreviewed"}], [{"id": 516, "state": "approved"}]], False),
    ([[{"id": 516}, {"id": 7, "re_requested": False}],
      [{"id": 516}, {"id": 7, "re_requested": True}]], False),
    ([[], None], False),
    ([[], [{"id": 516}], []], False),
])
async def test_reviewer_change_formats_and_transitions(monkeypatch, reviewers, expected):
    from pr_agent.servers import gitlab_webhook

    monkeypatch.setattr(gitlab_webhook, "_get_bot_user_id", mock.AsyncMock(return_value=516))
    assert await gitlab_webhook.is_bot_assigned_as_reviewer({"changes": {"reviewers": reviewers}}) is expected


@pytest.mark.parametrize("enabled, allowed, draft, expected", [
    (True, True, False, True),
    (False, True, False, False),
    (True, False, False, False),
    (True, True, True, False),
])
async def test_rerequest_dispatch_respects_existing_gates(monkeypatch, enabled, allowed, draft, expected):
    from pr_agent.servers import gitlab_webhook

    url = "https://gitlab.example.com/group/repo/-/merge_requests/1"
    payload = {
        "object_kind": "merge_request",
        "object_attributes": {"action": "update", "url": url},
        "changes": {"reviewers": [
            [{"id": 516, "re_requested": False}],
            [{"id": 516, "re_requested": True}],
        ]},
    }
    settings = mock.Mock()
    settings.get.side_effect = lambda key, default=None: {
        "gitlab.handle_reviewer_assignment": enabled,
        "gitlab.reviewer_commands": ["/review"],
    }.get(key, default)
    monkeypatch.setattr(gitlab_webhook, "get_settings", lambda: settings)
    monkeypatch.setattr(gitlab_webhook, "is_bot_user", lambda data: False)
    monkeypatch.setattr(gitlab_webhook, "get_include_pr_labels", lambda: False)
    monkeypatch.setattr(gitlab_webhook, "should_process_pr_logic", lambda data: allowed)
    monkeypatch.setattr(gitlab_webhook, "apply_repo_settings", lambda url: None)
    monkeypatch.setattr(gitlab_webhook, "is_draft", lambda data: draft)
    monkeypatch.setattr(gitlab_webhook, "_get_bot_user_id", mock.AsyncMock(return_value=516))
    dispatch = mock.AsyncMock()
    monkeypatch.setattr(gitlab_webhook, "_perform_commands_gitlab", dispatch)

    await gitlab_webhook.process_gitlab_event(payload, {})
    if expected:
        dispatch.assert_awaited_once()
        assert dispatch.await_args.args[0] == "reviewer_commands"
        assert dispatch.await_args.args[2] == url
    else:
        dispatch.assert_not_awaited()


class TestGetBotUserId:
    @staticmethod
    def _make_fake_gitlab(user_id):
        fake = mock.MagicMock()
        fake.Gitlab.return_value.auth.return_value = None
        fake.Gitlab.return_value.user.id = user_id
        return fake

    @staticmethod
    def _make_settings(url, token):
        s = mock.MagicMock()
        s.get.side_effect = lambda k, d=None: {
            "GITLAB.URL": url,
            "GITLAB.PERSONAL_ACCESS_TOKEN": token,
            "GITLAB.SSL_VERIFY": True,
            "GITLAB.AUTH_TYPE": "oauth_token",
        }.get(k, d)
        return s

    def test_caches_by_credential(self):
        from pr_agent.servers.gitlab_webhook import _bot_user_id_cache, _get_bot_user_id
        _bot_user_id_cache.clear()

        with mock.patch("pr_agent.servers.gitlab_webhook.get_settings",
                        return_value=self._make_settings("https://a.example.com", "token-a")):
            with mock.patch.dict("sys.modules", {"gitlab": self._make_fake_gitlab(111)}):
                assert asyncio.run(_get_bot_user_id()) == 111

        with mock.patch("pr_agent.servers.gitlab_webhook.get_settings",
                        return_value=self._make_settings("https://a.example.com", "token-b")):
            with mock.patch.dict("sys.modules", {"gitlab": self._make_fake_gitlab(222)}):
                assert asyncio.run(_get_bot_user_id()) == 222

        assert len(_bot_user_id_cache) >= 2

    def test_no_cache_on_failure(self):
        from pr_agent.servers.gitlab_webhook import _bot_user_id_cache, _get_bot_user_id
        _bot_user_id_cache.clear()

        fake = self._make_fake_gitlab(0)
        fake.Gitlab.side_effect = RuntimeError("auth failed")

        with mock.patch("pr_agent.servers.gitlab_webhook.get_settings",
                        return_value=self._make_settings("https://x.example.com", "fail-token")):
            with mock.patch.dict("sys.modules", {"gitlab": fake}):
                assert asyncio.run(_get_bot_user_id()) is None

        assert len(_bot_user_id_cache) == 0

    def test_respects_auth_type_private_token(self):
        from pr_agent.servers.gitlab_webhook import _bot_user_id_cache, _get_bot_user_id
        _bot_user_id_cache.clear()

        s = mock.MagicMock()
        s.get.side_effect = lambda k, d=None: {
            "GITLAB.URL": "https://x.example.com",
            "GITLAB.PERSONAL_ACCESS_TOKEN": "tok",
            "GITLAB.SSL_VERIFY": True,
            "GITLAB.AUTH_TYPE": "private_token",
        }.get(k, d)

        fake_gitlab = self._make_fake_gitlab(99)

        with mock.patch("pr_agent.servers.gitlab_webhook.get_settings", return_value=s):
            with mock.patch.dict("sys.modules", {"gitlab": fake_gitlab}):
                assert asyncio.run(_get_bot_user_id()) == 99

        call_kwargs = fake_gitlab.Gitlab.call_args.kwargs
        assert "private_token" in call_kwargs
        assert call_kwargs["private_token"] == "tok"

    def test_no_token_returns_none(self):
        from pr_agent.servers.gitlab_webhook import _bot_user_id_cache, _get_bot_user_id
        _bot_user_id_cache.clear()

        s = mock.MagicMock()
        s.get.side_effect = lambda k, d=None: {
            "GITLAB.URL": "https://x.example.com",
            "GITLAB.PERSONAL_ACCESS_TOKEN": None,
            "GITLAB.SSL_VERIFY": True,
            "GITLAB.AUTH_TYPE": "oauth_token",
        }.get(k, d)

        with mock.patch("pr_agent.servers.gitlab_webhook.get_settings", return_value=s):
            assert asyncio.run(_get_bot_user_id()) is None
