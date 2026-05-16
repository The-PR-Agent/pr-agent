import unittest
from unittest.mock import MagicMock, patch

from pr_agent.algo.best_practices import load_repo_best_practices_md


def _provider(returns):
    p = MagicMock(spec=["get_pr_agent_repo_custom_file"])
    p.get_pr_agent_repo_custom_file.return_value = returns
    return p


class _FakeContextProxy:
    """Module-level proxy that works as both subscriptable and attribute target."""

    def __init__(self):
        self._store = {}

    def get(self, key, default=None):
        return self._store.get(key, default)

    def __getitem__(self, key):
        return self._store[key]

    def __setitem__(self, key, value):
        self._store[key] = value

    def reset(self):
        self._store.clear()


class TestLoadRepoBestPracticesMd(unittest.TestCase):
    def setUp(self):
        self.fake_ctx = _FakeContextProxy()
        self.ctx_patch = patch(
            "pr_agent.algo.best_practices.context", self.fake_ctx
        )
        self.ctx_patch.start()

    def tearDown(self):
        self.ctx_patch.stop()

    @patch("pr_agent.algo.best_practices.get_settings")
    def test_enabled_by_default_with_content(self, mock_get_settings):
        s = MagicMock()
        s.get.side_effect = lambda key, default=None: {
            "best_practices.enable_repo_best_practices_md": True,
            "best_practices.repo_best_practices_md_path": "best_practices.md",
            "best_practices.max_lines_allowed": 800,
        }.get(key, default)
        mock_get_settings.return_value = s
        prov = _provider(b"# Best practices\n- rule 1\n- rule 2\n")
        out = load_repo_best_practices_md(prov)
        self.assertIn("rule 1", out)
        self.assertIn("rule 2", out)
        prov.get_pr_agent_repo_custom_file.assert_called_once_with("best_practices.md")

    @patch("pr_agent.algo.best_practices.get_settings")
    def test_opt_out_skips_fetch(self, mock_get_settings):
        s = MagicMock()
        s.get.side_effect = lambda key, default=None: {
            "best_practices.enable_repo_best_practices_md": False,
        }.get(key, default)
        mock_get_settings.return_value = s
        prov = _provider(b"should not be read")
        out = load_repo_best_practices_md(prov)
        self.assertEqual(out, "")
        prov.get_pr_agent_repo_custom_file.assert_not_called()

    @patch("pr_agent.algo.best_practices.get_settings")
    def test_file_absent_returns_empty(self, mock_get_settings):
        s = MagicMock()
        s.get.side_effect = lambda key, default=None: {
            "best_practices.enable_repo_best_practices_md": True,
            "best_practices.repo_best_practices_md_path": "best_practices.md",
            "best_practices.max_lines_allowed": 800,
        }.get(key, default)
        mock_get_settings.return_value = s
        prov = _provider(b"")
        out = load_repo_best_practices_md(prov)
        self.assertEqual(out, "")

    @patch("pr_agent.algo.best_practices.get_logger")
    @patch("pr_agent.algo.best_practices.get_settings")
    def test_truncation_emits_warning(self, mock_get_settings, mock_get_logger):
        s = MagicMock()
        s.get.side_effect = lambda key, default=None: {
            "best_practices.enable_repo_best_practices_md": True,
            "best_practices.repo_best_practices_md_path": "best_practices.md",
            "best_practices.max_lines_allowed": 5,
        }.get(key, default)
        mock_get_settings.return_value = s
        logger = MagicMock()
        mock_get_logger.return_value = logger
        body = "\n".join(f"line {i}" for i in range(20))
        prov = _provider(body.encode("utf-8"))
        out = load_repo_best_practices_md(prov)
        self.assertEqual(len(out.splitlines()), 5)
        # WARNING message about truncation must include the from/to counts.
        warning_msgs = [c.args[0] for c in logger.warning.call_args_list]
        self.assertTrue(any("Truncating" in m and "20" in m and "5" in m for m in warning_msgs),
                        f"warning not emitted: {warning_msgs}")
        # INFO log emitted on fetch.
        info_msgs = [c.args[0] for c in logger.info.call_args_list]
        self.assertTrue(any("Loaded" in m for m in info_msgs))

    @patch("pr_agent.algo.best_practices.get_settings")
    def test_caches_across_calls(self, mock_get_settings):
        s = MagicMock()
        s.get.side_effect = lambda key, default=None: {
            "best_practices.enable_repo_best_practices_md": True,
            "best_practices.repo_best_practices_md_path": "best_practices.md",
            "best_practices.max_lines_allowed": 800,
        }.get(key, default)
        mock_get_settings.return_value = s
        prov = _provider(b"hello\n")
        first = load_repo_best_practices_md(prov)
        second = load_repo_best_practices_md(prov)
        self.assertEqual(first, second)
        prov.get_pr_agent_repo_custom_file.assert_called_once()

    @patch("pr_agent.algo.best_practices.get_settings")
    def test_str_return_tolerated(self, mock_get_settings):
        s = MagicMock()
        s.get.side_effect = lambda key, default=None: {
            "best_practices.enable_repo_best_practices_md": True,
            "best_practices.repo_best_practices_md_path": "best_practices.md",
            "best_practices.max_lines_allowed": 800,
        }.get(key, default)
        mock_get_settings.return_value = s
        prov = _provider("text content\n")
        out = load_repo_best_practices_md(prov)
        self.assertIn("text content", out)

    @patch("pr_agent.algo.best_practices.get_logger")
    @patch("pr_agent.algo.best_practices.get_settings")
    def test_invalid_max_lines_falls_back(self, mock_get_settings, mock_get_logger):
        s = MagicMock()
        s.get.side_effect = lambda key, default=None: {
            "best_practices.enable_repo_best_practices_md": True,
            "best_practices.repo_best_practices_md_path": "best_practices.md",
            "best_practices.max_lines_allowed": "not-a-number",
        }.get(key, default)
        mock_get_settings.return_value = s
        logger = MagicMock()
        mock_get_logger.return_value = logger
        prov = _provider(b"line\n")
        out = load_repo_best_practices_md(prov)
        self.assertEqual(out, "line")
        warning_msgs = [c.args[0] for c in logger.warning.call_args_list]
        self.assertTrue(
            any("Invalid best_practices.max_lines_allowed" in m for m in warning_msgs),
            f"fallback warning not emitted: {warning_msgs}",
        )


if __name__ == "__main__":
    unittest.main()
