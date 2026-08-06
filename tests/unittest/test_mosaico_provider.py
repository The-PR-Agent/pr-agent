"""Tests for DiffInputProvider + provider_registration: drive /review and /improve
end-to-end through DiffInputProvider with a mocked LLM, assert non-empty captured
artifact, no un-stubbed provider method raises, and the incremental path never fires.

asyncio_mode=auto."""
import pytest

from pr_agent.algo.types import EDIT_TYPE
from pr_agent.config_loader import get_settings, global_settings
from pr_agent.mosaico.diff_provider import DiffInputProvider, parse_unified_diff

TWO_FILE_DIFF = """diff --git a/added.py b/added.py
new file mode 100644
index 0000000..e69de29
--- /dev/null
+++ b/added.py
@@ -0,0 +1,3 @@
+def hello():
+    return "world"
+
diff --git a/existing.py b/existing.py
index 1111111..2222222 100644
--- a/existing.py
+++ b/existing.py
@@ -1,4 +1,4 @@
 import os
-x = 1
+x = 2
 y = 3
"""


class TestParseUnifiedDiff:
    def test_two_file_blob(self):
        files = parse_unified_diff(TWO_FILE_DIFF)
        assert len(files) == 2

        added, existing = files[0], files[1]
        assert added.filename == "added.py"
        assert added.edit_type == EDIT_TYPE.ADDED
        assert "def hello():" in added.patch
        assert added.patch.startswith("diff --git a/added.py b/added.py")

        assert existing.filename == "existing.py"
        assert existing.edit_type == EDIT_TYPE.MODIFIED
        # patch preserved verbatim for the section
        assert "-x = 1" in existing.patch
        assert "+x = 2" in existing.patch

    def test_head_base_reconstruction(self):
        files = parse_unified_diff(TWO_FILE_DIFF)
        existing = files[1]
        # head has the new line, base has the old
        assert "x = 2" in existing.head_file
        assert "x = 1" in existing.base_file
        # context lines preserved in both
        assert "import os" in existing.head_file
        assert "import os" in existing.base_file

    def test_no_header_returns_empty(self):
        assert parse_unified_diff("just some text, no diff") == []
        assert parse_unified_diff("") == []
        assert parse_unified_diff(None) == []

    def test_rename_detected(self):
        rename_diff = (
            "diff --git a/old.py b/new.py\n"
            "similarity index 100%\n"
            "rename from old.py\n"
            "rename to new.py\n"
        )
        files = parse_unified_diff(rename_diff)
        assert len(files) == 1
        assert files[0].edit_type == EDIT_TYPE.RENAMED
        assert files[0].filename == "new.py"
        assert files[0].old_filename == "old.py"


class TestProviderRegistration:
    def test_registry_has_mosaico_diff_and_originals_intact(self):
        import pr_agent.mosaico.provider_registration  # noqa: F401 (triggers setdefault)
        from pr_agent.git_providers import _GIT_PROVIDERS
        assert _GIT_PROVIDERS.get("mosaico_diff") is DiffInputProvider
        # original keys intact (setdefault did not clobber)
        for key in ("github", "gitlab", "bitbucket", "azure", "local", "gerrit", "gitea"):
            assert key in _GIT_PROVIDERS

    def test_setdefault_does_not_clobber(self):
        from pr_agent.git_providers import _GIT_PROVIDERS
        sentinel = object()
        original = _GIT_PROVIDERS.get("mosaico_diff")
        try:
            _GIT_PROVIDERS["mosaico_diff"] = sentinel
            _GIT_PROVIDERS.setdefault("mosaico_diff", DiffInputProvider)
            assert _GIT_PROVIDERS["mosaico_diff"] is sentinel
        finally:
            if original is not None:
                _GIT_PROVIDERS["mosaico_diff"] = original


class TestDiffInputProviderBasics:
    def test_instantiable_and_is_supported_false(self, mosaico_input):
        p = DiffInputProvider("pr_url_unused")
        assert p.is_supported("gfm_markdown") is False
        assert p.is_supported("anything") is False

    def test_input_methods(self, mosaico_input):
        p = DiffInputProvider()
        assert [f.filename for f in p.get_diff_files()] == ["added.py", "existing.py"]
        assert p.get_files() == ["added.py", "existing.py"]
        assert p.get_languages() == {"Python": 100}
        assert p.get_pr_description_full() == ""
        assert p.pr.title == "test PR"
        assert p.pr.diff_files == p.get_diff_files()


_SENTINEL = object()


def _snapshot_mosaico_input():
    """Capture MOSAICO.INPUT state precisely: whether the MOSAICO key existed, whether
    its INPUT child existed, and the prior value. Returned tuple feeds _restore_mosaico_input."""
    mosaico_existed = "MOSAICO" in global_settings
    input_value = global_settings.get("MOSAICO.INPUT", _SENTINEL)
    return mosaico_existed, input_value


def _restore_mosaico_input(snapshot):
    """Leave global_settings EXACTLY as found: if MOSAICO did not exist, remove it; if
    MOSAICO existed but INPUT did not, drop only INPUT; otherwise restore the prior value."""
    mosaico_existed, input_value = snapshot
    if not mosaico_existed:
        global_settings.unset("MOSAICO")
        return
    if input_value is _SENTINEL:
        box = global_settings.get("MOSAICO")
        if box is not None and hasattr(box, "pop"):
            box.pop("INPUT", None)
    else:
        global_settings.set("MOSAICO.INPUT", input_value)


@pytest.fixture
def mosaico_input():
    """Set MOSAICO.INPUT on global_settings (no context active) and restore exactly after."""
    files = parse_unified_diff(TWO_FILE_DIFF)
    snapshot = _snapshot_mosaico_input()
    global_settings.set("MOSAICO.INPUT", {
        "files": files,
        "languages": {"Python": 100},
        "title": "test PR",
    })
    yield files
    _restore_mosaico_input(snapshot)


# ---------------------------------------------------------------------------
# Drive /review and /improve end-to-end through DiffInputProvider.
# ---------------------------------------------------------------------------

_INCREMENTAL_ONLY_METHODS = (
    "get_incremental_commits", "unreviewed_files_set", "previous_review", "auto_approve",
)


class _SpyProvider(DiffInputProvider):
    """Traps access to incremental-only attributes to prove they are never used."""
    incremental_access = None

    def __getattribute__(self, name):
        if name in _INCREMENTAL_ONLY_METHODS:
            # record the access on the class-level list; resolution still proceeds normally
            # (these attrs are not defined on DiffInputProvider, so an actual access would
            # AttributeError on its own — the recording just makes the violation explicit).
            lst = type(self).incremental_access
            if lst is not None:
                lst.append(name)
        return super().__getattribute__(name)


CANNED_REVIEW_YAML = """\
review:
  estimated_effort_to_review_[1-5]: '2'
  score: '85'
  relevant_tests: 'No'
  key_issues_to_review:
    - relevant_file: existing.py
      issue_header: 'Possible Bug'
      issue_content: 'x changed from 1 to 2'
      start_line: 1
      end_line: 1
  security_concerns: 'No'
"""

CANNED_IMPROVE_YAML = """\
code_suggestions:
  - relevant_file: existing.py
    language: python
    existing_code: |
      x = 2
    suggestion_content: 'Consider a named constant instead of a magic number'
    improved_code: |
      X = 2
    one_sentence_summary: 'Use a named constant'
    label: 'best practice'
"""

# The /improve flow makes a mandatory second LLM call (self-reflection) that scores
# each suggestion and injects relevant_lines_start/end. The mock returns this on call 2.
CANNED_IMPROVE_REFLECTION_YAML = """\
code_suggestions:
  - suggestion_score: 8
    why: 'Improves readability'
    relevant_lines_start: 1
    relevant_lines_end: 1
"""


def _setup_request_settings(verb_provider_input):
    """Mimic the per-request isolation the executor will do: deepcopy global_settings
    into the (module-global, since no context) settings, set GIT_PROVIDER + MOSAICO.INPUT
    + publish_output=False."""
    s = get_settings()
    s.set("CONFIG.GIT_PROVIDER", "mosaico_diff")
    s.set("CONFIG.PUBLISH_OUTPUT", False)
    s.set("MOSAICO.INPUT", verb_provider_input)
    # clear any stale capture (Dynaconf merges dict assignment; blank the artifact explicitly)
    s.data = {"artifact": ""}


@pytest.fixture
def isolated_settings():
    """Snapshot/restore the settings keys these tests mutate, leaving global_settings
    exactly as found (incl. MOSAICO.INPUT present/absent state)."""
    keys = ["CONFIG.GIT_PROVIDER", "CONFIG.PUBLISH_OUTPUT"]
    sentinel = object()
    before = {k: global_settings.get(k, sentinel) for k in keys}
    mosaico_snapshot = _snapshot_mosaico_input()
    data_before = global_settings.get("data", sentinel)
    yield
    for k, v in before.items():
        if v is not sentinel:
            global_settings.set(k, v)
    _restore_mosaico_input(mosaico_snapshot)
    # restore data (Dynaconf merges dict assignment; blank the artifact when absent before)
    if data_before is sentinel:
        global_settings.data = {"artifact": ""}
    else:
        global_settings.data = data_before


async def _run_tool_via_spy(monkeypatch, isolated_settings, verb, canned_yaml, second_yaml=""):
    """Patch the provider registry to a spy subclass, mock the LLM, run the tool with
    publish_output=False, and return (captured_artifact, incremental_accessed_list)."""
    from pr_agent.git_providers import _GIT_PROVIDERS
    import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_mod

    files = parse_unified_diff(TWO_FILE_DIFF)
    provider_input = {"files": files, "languages": {"Python": 100}, "title": "spike PR"}
    _setup_request_settings(provider_input)

    incremental_access = []
    _SpyProvider.incremental_access = incremental_access
    monkeypatch.setitem(_GIT_PROVIDERS, "mosaico_diff", _SpyProvider)

    # The first LLM call is the tool's main prediction (return the canned YAML).
    # Subsequent calls (e.g. /improve's self-reflection scoring) get an empty string,
    # which makes the tool fall back to its default suggestion score (passes threshold).
    call_count = {"n": 0}

    async def fake_chat_completion(self, model, system, user, temperature=0.2, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return canned_yaml, "stop"
        return second_yaml, "stop"

    monkeypatch.setattr(litellm_mod.LiteLLMAIHandler, "chat_completion", fake_chat_completion)

    from pr_agent.agent.pr_agent import PRAgent
    ok = await PRAgent().handle_request("mosaico://supplied-diff", ["/" + verb])

    artifact = (get_settings().get("data", {}) or {}).get("artifact", "")
    _SpyProvider.incremental_access = None
    return ok, artifact, incremental_access


class TestSpikeEndToEnd:
    @pytest.mark.asyncio
    async def test_review_end_to_end(self, monkeypatch, isolated_settings):
        ok, artifact, incremental = await _run_tool_via_spy(
            monkeypatch, isolated_settings, "review", CANNED_REVIEW_YAML)
        assert ok is True, "review handle_request returned False (swallowed exception)"
        assert artifact, "review produced empty artifact"
        assert incremental == [], f"incremental-only methods were accessed: {incremental}"

    @pytest.mark.asyncio
    async def test_improve_end_to_end(self, monkeypatch, isolated_settings):
        ok, artifact, incremental = await _run_tool_via_spy(
            monkeypatch, isolated_settings, "improve", CANNED_IMPROVE_YAML,
            second_yaml=CANNED_IMPROVE_REFLECTION_YAML)
        assert ok is True, "improve handle_request returned False (swallowed exception)"
        assert artifact, "improve produced empty artifact"
        assert incremental == [], f"incremental-only methods were accessed: {incremental}"
