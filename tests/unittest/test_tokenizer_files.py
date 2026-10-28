import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
import tiktoken.registry

from pr_agent.algo import token_handler

LITELLM_TOKENIZERS = Path(
    importlib.util.find_spec("litellm").submodule_search_locations[0], "litellm_core_utils", "tokenizers"
)


def _settings():
    return SimpleNamespace(config=SimpleNamespace(model="primary-model"))


@pytest.fixture
def offline(monkeypatch):
    """Block tiktoken's own download and drop encodings tiktoken already built in-process."""
    def no_network(*args, **kwargs):
        raise AssertionError("tiktoken tried to download")

    monkeypatch.setattr(requests, "get", no_network)
    monkeypatch.setattr(tiktoken.registry, "ENCODINGS", {})


@pytest.fixture
def clean_cache_env(monkeypatch):
    for name in ("TIKTOKEN_CACHE_DIR", "DATA_GYM_CACHE_DIR", "CUSTOM_TIKTOKEN_CACHE_DIR"):
        monkeypatch.delenv(name, raising=False)


def test_defaults_to_litellm_bundled_files(clean_cache_env):
    token_handler._use_bundled_tiktoken_files()

    assert os.environ["TIKTOKEN_CACHE_DIR"] == str(LITELLM_TOKENIZERS)


@pytest.mark.parametrize("variable", ["TIKTOKEN_CACHE_DIR", "DATA_GYM_CACHE_DIR"])
def test_keeps_host_chosen_cache(clean_cache_env, monkeypatch, variable):
    monkeypatch.setenv(variable, "/host/cache")

    token_handler._use_bundled_tiktoken_files()

    assert os.environ[variable] == "/host/cache"
    assert variable == "TIKTOKEN_CACHE_DIR" or "TIKTOKEN_CACHE_DIR" not in os.environ


def test_honours_litellm_custom_cache_variable(clean_cache_env, monkeypatch):
    monkeypatch.setenv("CUSTOM_TIKTOKEN_CACHE_DIR", "/custom/cache")

    token_handler._use_bundled_tiktoken_files()

    assert os.environ["TIKTOKEN_CACHE_DIR"] == "/custom/cache"


@pytest.mark.parametrize(("model", "encoding"), [
    ("gpt-4o", "o200k_base"),
    ("gpt-4", "cl100k_base"),
    ("claude-sonnet-4", "o200k_base"),
    ("openai/my-corp-model", "o200k_base"),
])
def test_bundled_encoders_load_without_network(monkeypatch, offline, model, encoding):
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(LITELLM_TOKENIZERS))
    monkeypatch.setattr(token_handler, "get_settings", lambda use_context=True: _settings())

    assert token_handler.TokenEncoder._create_encoder(model).name == encoding
