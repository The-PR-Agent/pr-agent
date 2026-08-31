import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

from pr_agent.algo import token_handler


def test_oversized_claude_patch_falls_back_to_local_estimate(monkeypatch):
    settings = SimpleNamespace(
        config=SimpleNamespace(model="claude-3-7-sonnet-20250219"),
        get=lambda key, default=None: {
            "anthropic.key": "test-key",
            "config.model_token_count_estimate_factor": 0.3,
        }.get(key, default),
    )
    monkeypatch.setattr(
        token_handler, "get_settings", lambda use_context=False: settings
    )

    client = MagicMock()
    anthropic_module = types.ModuleType("anthropic")
    anthropic_module.Anthropic = MagicMock(return_value=client)
    monkeypatch.setitem(sys.modules, "anthropic", anthropic_module)

    handler = token_handler.TokenHandler.__new__(token_handler.TokenHandler)
    handler.encoder = MagicMock()
    handler.encoder.encode.return_value = [0] * 10
    handler.CLAUDE_MAX_CONTENT_SIZE = 3

    assert handler.count_tokens("abcd", force_accurate=True) == 13
    client.messages.count_tokens.assert_not_called()
