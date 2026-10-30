"""scripts/structural_reach_producer.py skips a malformed tsconfig file with a warning instead of stopping."""
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "structural_reach_producer.py"


def _load_producer():
    # tree-sitter-language-pack is installed only where the producer runs, not as a pr-agent dependency.
    stub = ModuleType("tree_sitter_language_pack")
    stub.get_parser = lambda language: None
    spec = importlib.util.spec_from_file_location("structural_reach_producer", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"tree_sitter_language_pack": stub}):
        spec.loader.exec_module(module)
    return module


producer = _load_producer()


def _ts_paths(monkeypatch, tsconfig):
    monkeypatch.setattr(producer, "cat_blobs", lambda repo, oids: [(oid, tsconfig.encode()) for oid in oids])
    return producer.load_ts_paths(".", {"tsconfig.json": "oid"})


VALID = '{\n  // comment\n  "compilerOptions": {"baseUrl": "src", "paths": {"@app/*": ["app/*"],},},\n}'


def test_a_valid_tsconfig_with_comments_and_trailing_commas_gives_its_paths(monkeypatch, capsys):
    assert _ts_paths(monkeypatch, VALID) == {"@app/*": ["src/app/*"]}
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("tsconfig, reason", [
    ('{"compilerOptions": {"paths": {"@a/*": ["a/*"]}} /* unterminated', "not valid JSON"),
    ('{"compilerOptions": {"baseUrl": "src\\', "not valid JSON"),
    ('[{"compilerOptions": {}}]', "the top level is not an object"),
    ('{"compilerOptions": null}', "compilerOptions is not an object"),
    ('{"compilerOptions": {"baseUrl": null}}', "compilerOptions.baseUrl is not a string"),
    ('{"compilerOptions": {"paths": [["a/*"]]}}', "compilerOptions.paths is not an object of string arrays"),
    ('{"compilerOptions": {"paths": {"@a/*": "a/*"}}}', "compilerOptions.paths is not an object of string arrays"),
    ('{"compilerOptions": {"paths": {"@a/*": [1]}}}', "compilerOptions.paths is not an object of string arrays"),
])
def test_a_malformed_tsconfig_is_skipped_with_a_warning(monkeypatch, capsys, tsconfig, reason):
    assert _ts_paths(monkeypatch, tsconfig) == {}
    assert f"warning: skipping tsconfig.json: {reason}" in capsys.readouterr().err


def test_a_tsconfig_without_compiler_options_is_not_a_warning(monkeypatch, capsys):
    assert _ts_paths(monkeypatch, "{}") == {}
    assert capsys.readouterr().err == ""
