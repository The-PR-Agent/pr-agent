"""Tests for the standalone structural-reach producer."""
import importlib.util
import sys
from collections import Counter
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


@pytest.mark.parametrize("reverse_order", [False, True])
def test_shared_blobs_preserve_each_language_and_path_with_a_reusable_cache(monkeypatch, reverse_order):
    source = b'import "../shared/dep";'
    blobs = {"shared": source, "empty": b""}
    tree = {
        "js/src/a.js": "shared", "js/shared/dep.js": "empty",
        "alias/src/b.js": "shared", "alias/shared/dep.js": "empty",
        "ts/src/a.ts": "shared", "ts/shared/dep.ts": "empty",
    }
    if reverse_order:
        tree = dict(reversed(list(tree.items())))
    reads, parses = [], []

    def cat_blobs(repo, oids):
        reads.extend(oids)
        return [(oid, blobs[oid]) for oid in oids]

    def extract_imports(content, language):
        parses.append((content, language))
        return ["../shared/dep"] if content == source else []

    monkeypatch.setattr(producer, "cat_blobs", cat_blobs)
    monkeypatch.setattr(producer, "extract_imports", extract_imports)
    cache = {"unrelated": b"keep"}
    graph = producer.ImportGraph(".", tree, cache)
    assert parses[0][1] == ("typescript" if reverse_order else "javascript")

    for root, importer in (("js", "a.js"), ("ts", "a.ts"), ("alias", "b.js")):
        changed = [f"{root}/shared/dep.{root if root == 'ts' else 'js'}"]
        assert graph.affected(changed) == {*changed, f"{root}/src/{importer}"}
        assert producer.structural_reach(graph, changed, "module", 1) == {
            "version": 1, "reach": 2, "reach_unit": "modules",
            "dependents": [f"{root}/shared", f"{root}/src"],
            "domains": [root], "cross_domain": False,
        }
    assert Counter(reads) == Counter({"shared": 1, "empty": 1})
    assert Counter(parses) == Counter({
        (content, language): 1 for content in blobs.values() for language in ("javascript", "typescript")
    })
    assert cache == {
        "unrelated": b"keep",
        ("shared", "javascript"): ["../shared/dep"], ("shared", "typescript"): ["../shared/dep"],
        ("empty", "javascript"): [], ("empty", "typescript"): [],
    }

    warm_graph = producer.ImportGraph(".", tree, cache)
    assert warm_graph.reverse == graph.reverse
    assert len(reads) == 2
    assert len(parses) == 4


def test_parse_failure_preserves_completed_results_without_raw_cache_entries(monkeypatch):
    blobs = {"first": b'import "./dep";', "second": b'import "./other";'}
    failure = RuntimeError("parse failed")

    def extract_imports(content, language):
        if content == blobs["second"]:
            raise failure
        return ["./dep"]

    monkeypatch.setattr(producer, "cat_blobs", lambda repo, oids: [(oid, blobs[oid]) for oid in oids])
    monkeypatch.setattr(producer, "extract_imports", extract_imports)
    cache = {"unrelated": b"keep"}
    with pytest.raises(RuntimeError) as caught:
        producer.ImportGraph(".", {"first.js": "first", "second.ts": "second"}, cache)

    assert caught.value is failure
    assert cache == {"unrelated": b"keep", ("first", "javascript"): ["./dep"]}
