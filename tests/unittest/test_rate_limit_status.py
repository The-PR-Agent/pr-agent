from unittest.mock import Mock

import pr_agent.algo.utils as utils


class TestRateLimitStatus:
    def test_first_request_failure_is_retried(self, monkeypatch):
        settings = Mock()
        settings.get.return_value = "https://api.github.com"
        monkeypatch.setattr(utils, "get_settings", lambda use_context=False: settings)
        monkeypatch.setattr(utils.time, "sleep", lambda _: None)

        response = Mock()
        response.json.return_value = {"resources": {"core": {"remaining": 1}}}
        mock_get = Mock(side_effect=[Exception("timeout"), response])
        monkeypatch.setattr(utils.requests, "get", mock_get)

        assert utils.get_rate_limit_status("token") == {"resources": {"core": {"remaining": 1}}}
        assert mock_get.call_count == 2
