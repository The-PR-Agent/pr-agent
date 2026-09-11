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
