from unittest.mock import Mock

import pytest

import pr_agent.algo.utils as utils


class TestRateLimitStatus:
    def _patch_env(self, monkeypatch):
        settings = Mock()
        settings.get.return_value = "https://api.github.com"
        monkeypatch.setattr(utils, "get_settings", lambda use_context=False: settings)
        monkeypatch.setattr(utils.time, "sleep", lambda _: None)

    def test_first_request_failure_is_retried(self, monkeypatch):
        self._patch_env(monkeypatch)

        response = Mock()
        response.json.return_value = {"resources": {"core": {"remaining": 1}}}
        mock_get = Mock(side_effect=[Exception("timeout"), response])
        monkeypatch.setattr(utils.requests, "get", mock_get)

        assert utils.get_rate_limit_status("token") == {"resources": {"core": {"remaining": 1}}}
        assert mock_get.call_count == 2

    def test_retry_normalizes_github_enterprise_response(self, monkeypatch):
        """The retry attempt must go through the same handling as the first one"""
        self._patch_env(monkeypatch)

        response = Mock()
        response.json.return_value = {"message": "Rate limiting is not enabled."}
        mock_get = Mock(side_effect=[Exception("timeout"), response])
        monkeypatch.setattr(utils.requests, "get", mock_get)

        assert utils.get_rate_limit_status("token") == {"resources": {}}
        assert mock_get.call_count == 2

    def test_persistent_failure_raises(self, monkeypatch):
        self._patch_env(monkeypatch)

        mock_get = Mock(side_effect=[Exception("boom"), Exception("boom")])
        monkeypatch.setattr(utils.requests, "get", mock_get)

        with pytest.raises(Exception, match="boom"):
            utils.get_rate_limit_status("token")
        assert mock_get.call_count == 2
