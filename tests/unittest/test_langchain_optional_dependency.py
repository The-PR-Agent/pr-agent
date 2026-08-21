"""Import the LangChain handler without requiring the optional LangChain packages."""
import builtins
import importlib
import sys

import pytest

MODULE = "pr_agent.algo.ai_handlers.langchain_ai_handler"


@pytest.fixture
def without_langchain(monkeypatch):
    """Simulate an environment where langchain is not installed."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("langchain"):
            raise ModuleNotFoundError(f"No module named '{name}'")
        return real_import(name, *args, **kwargs)

    for mod in [m for m in list(sys.modules) if m.startswith("langchain")]:
        monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.delitem(sys.modules, MODULE, raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    yield


def test_import_the_handler_module_without_langchain(without_langchain):
    """Import the module so the rest of pr-agent is unaffected by the missing extra."""
    module = importlib.import_module(MODULE)

    assert module._LANGCHAIN_INSTALLED is False


def test_constructing_the_handler_raises_a_helpful_import_error(without_langchain):
    """Reach the documented guard, so construction fails rather than the import."""
    module = importlib.import_module(MODULE)

    with pytest.raises(ImportError, match="LangChain is not installed"):
        module.LangChainOpenAIHandler()
