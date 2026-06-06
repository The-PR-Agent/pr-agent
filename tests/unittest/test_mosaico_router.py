"""Tests for the MOSAICO dispatch router (capture/fallback behavior).

Pins: each path returns a string and NEVER raises; no-files/no-suggestions/empty-ask
-> empty-fallback string; a tool that raises (monkeypatched) -> error-fallback string
with no exception escaping.

asyncio_mode=auto."""
import pytest

from pr_agent.config_loader import global_settings
from pr_agent.mosaico.dispatch import (_detect_verb, _empty_fallback,
                                       _error_fallback, route_and_run)

PR_URL = "https://github.com/org/repo/pull/123"

SAMPLE_DIFF = """```diff
diff --git a/foo.py b/foo.py
index 1111111..2222222 100644
--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,2 @@
-x = 1
+x = 2
 y = 3
```"""

_SENTINEL = object()


@pytest.fixture
def restore_settings():
    """Snapshot/restore the settings keys the router mutates, leaving global_settings
    exactly as found."""
    keys = ["CONFIG.GIT_PROVIDER", "CONFIG.PUBLISH_OUTPUT", "CONFIG.PUBLISH_OUTPUT_PROGRESS"]
    before = {k: global_settings.get(k, _SENTINEL) for k in keys}
    mosaico_existed = "MOSAICO" in global_settings
    mosaico_input = global_settings.get("MOSAICO.INPUT", _SENTINEL)
    data_before = global_settings.get("data", _SENTINEL)
    yield
    for k, v in before.items():
        if v is not _SENTINEL:
            global_settings.set(k, v)
    if not mosaico_existed:
        global_settings.unset("MOSAICO")
    elif mosaico_input is _SENTINEL:
        box = global_settings.get("MOSAICO")
        if box is not None and hasattr(box, "pop"):
            box.pop("INPUT", None)
    else:
        global_settings.set("MOSAICO.INPUT", mosaico_input)
    # Dynaconf merges dict assignments; explicitly blank the artifact when it was absent.
    if data_before is _SENTINEL:
        global_settings.data = {"artifact": ""}
    else:
        global_settings.data = data_before


def _set_artifact(value):
    # Mirror how the real tools stash output: attribute assignment replaces cleanly
    # (Dynaconf .set merges dicts, which would not overwrite a prior artifact).
    global_settings.data = {"artifact": value}


def _clear_artifact():
    # Dynaconf merges dict assignments, so an empty {} would not drop a prior 'artifact'.
    # Set the artifact explicitly empty (this is also the legitimate no-output state).
    global_settings.data = {"artifact": ""}


# ---------------------------------------------------------------------------
# Verb detection
# ---------------------------------------------------------------------------
class TestVerbDetection:
    def test_explicit_slash_verbs(self):
        assert _detect_verb("/review please") == "review"
        assert _detect_verb("/improve this") == "improve"
        assert _detect_verb("/describe it") == "describe"
        assert _detect_verb("/ask something") == "ask"

    def test_bare_verb_words(self):
        assert _detect_verb("review this PR") == "review"
        assert _detect_verb("improve the code") == "improve"

    def test_question_defaults_to_ask(self):
        assert _detect_verb("what does this change?") == "ask"
        assert _detect_verb("How is the error handled") == "ask"

    def test_default_is_review(self):
        assert _detect_verb("here is a blob of stuff") == "review"


# ---------------------------------------------------------------------------
# Path (a): host PR URL
# ---------------------------------------------------------------------------
class TestPathPrUrl:
    @pytest.mark.asyncio
    async def test_pr_url_runs_handle_request_and_returns_artifact(self, monkeypatch, restore_settings):
        captured = {}

        async def fake_handle_request(self, pr_url, request, notify=None):
            captured["pr_url"] = pr_url
            captured["request"] = request
            _set_artifact("REVIEW MARKDOWN")
            return True

        from pr_agent.agent.pr_agent import PRAgent
        monkeypatch.setattr(PRAgent, "handle_request", fake_handle_request)

        out = await route_and_run(f"review {PR_URL}")
        assert out == "REVIEW MARKDOWN"
        assert captured["pr_url"] == PR_URL
        # _run_pr_agent must inject the no-publish flags so tools write into
        # data["artifact"] instead of publishing to the real PR.
        assert "--config.publish_output=false" in captured["request"]
        assert "--config.publish_output_progress=false" in captured["request"]
        assert "/review" in captured["request"]

    @pytest.mark.asyncio
    async def test_pr_url_leaves_git_provider_default(self, monkeypatch, restore_settings):
        global_settings.set("CONFIG.GIT_PROVIDER", "github")

        async def fake_handle_request(self, pr_url, request, notify=None):
            _set_artifact("OK")
            return True

        from pr_agent.agent.pr_agent import PRAgent
        monkeypatch.setattr(PRAgent, "handle_request", fake_handle_request)

        await route_and_run(f"review {PR_URL}")
        # path (a) must NOT switch the provider to mosaico_diff
        assert global_settings.get("CONFIG.GIT_PROVIDER") == "github"


# ---------------------------------------------------------------------------
# Path (b): supplied diff
# ---------------------------------------------------------------------------
class TestPathSuppliedDiff:
    @pytest.mark.asyncio
    async def test_diff_sets_provider_and_input(self, monkeypatch, restore_settings):
        captured = {}

        async def fake_handle_request(self, pr_url, request, notify=None):
            # read the context (here: global) settings the router set
            captured["git_provider"] = global_settings.get("CONFIG.GIT_PROVIDER")
            captured["mosaico_input"] = global_settings.get("MOSAICO.INPUT")
            captured["request"] = request
            _set_artifact("DIFF REVIEW")
            return True

        from pr_agent.agent.pr_agent import PRAgent
        monkeypatch.setattr(PRAgent, "handle_request", fake_handle_request)

        out = await route_and_run(f"review the following\n{SAMPLE_DIFF}")
        assert out == "DIFF REVIEW"
        assert captured["git_provider"] == "mosaico_diff"
        assert "/review" in captured["request"]
        assert "--config.publish_output=false" in captured["request"]
        assert "--config.publish_output_progress=false" in captured["request"]
        mi = captured["mosaico_input"]
        assert mi and [f.filename for f in mi["files"]] == ["foo.py"]
        assert mi["title"] == "Supplied diff"

    @pytest.mark.asyncio
    async def test_unparseable_diff_returns_empty_fallback(self, monkeypatch, restore_settings):
        # looks like a diff (has a fence) but parse yields nothing
        out = await route_and_run("```diff\nnot really a diff\n```")
        assert out == _empty_fallback("review")

    @pytest.mark.asyncio
    async def test_diff_with_question_mark_in_body_still_reviews(self, monkeypatch, restore_settings):
        """A '?' inside the patch (ternary/regex/comment) must NOT flip the supplied-diff
        default from review to ask. PRQuestions must never be touched here."""
        captured = {}

        async def fake_handle_request(self, pr_url, request, notify=None):
            captured["request"] = request
            _set_artifact("DIFF REVIEW")
            return True

        def _explode_prquestions(*a, **k):
            raise AssertionError("ask path taken for a diff whose '?' is only in the body")

        from pr_agent.agent.pr_agent import PRAgent
        monkeypatch.setattr(PRAgent, "handle_request", fake_handle_request)
        monkeypatch.setattr("pr_agent.tools.pr_questions.PRQuestions", _explode_prquestions)

        diff_with_q = (
            "```diff\n"
            "diff --git a/foo.py b/foo.py\n"
            "index 1111111..2222222 100644\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-y = a if b else c\n"
            "+y = a ? b : c  # is this right?\n"
            " z = 3\n"
            "```"
        )
        out = await route_and_run(diff_with_q)
        assert out == "DIFF REVIEW"
        assert "/review" in captured["request"]


# ---------------------------------------------------------------------------
# Path (a)/(b) ask: PRQuestions IS invoked when a PR URL or a diff is present
# ---------------------------------------------------------------------------
class TestAskWithContext:
    @pytest.mark.asyncio
    async def test_pr_url_question_runs_prquestions(self, monkeypatch, restore_settings):
        captured = {}

        class FakePRQuestions:
            def __init__(self, pr_url, args=None, ai_handler=None):
                captured["pr_url"] = pr_url
                captured["args"] = args
                self.prediction = "URL ANSWER"

            async def run(self):
                return ""

        pr_agent_used = {"called": False}

        async def fail_handle_request(self, *a, **k):
            pr_agent_used["called"] = True
            return True

        monkeypatch.setattr("pr_agent.tools.pr_questions.PRQuestions", FakePRQuestions)
        from pr_agent.agent.pr_agent import PRAgent
        monkeypatch.setattr(PRAgent, "handle_request", fail_handle_request)

        out = await route_and_run(f"what does this change? {PR_URL}")
        assert out == "URL ANSWER"
        assert captured["pr_url"] == PR_URL  # path (a): URL drives the provider target
        assert pr_agent_used["called"] is False

    @pytest.mark.asyncio
    async def test_supplied_diff_question_runs_prquestions(self, monkeypatch, restore_settings):
        captured = {}

        class FakePRQuestions:
            def __init__(self, pr_url, args=None, ai_handler=None):
                captured["pr_url"] = pr_url
                captured["git_provider"] = global_settings.get("CONFIG.GIT_PROVIDER")
                self.prediction = "DIFF ANSWER"

            async def run(self):
                return ""

        monkeypatch.setattr("pr_agent.tools.pr_questions.PRQuestions", FakePRQuestions)

        out = await route_and_run(f"what changed here?\n{SAMPLE_DIFF}")
        assert out == "DIFF ANSWER"
        assert captured["pr_url"] == "mosaico://supplied-diff"  # path (b) target
        assert captured["git_provider"] == "mosaico_diff"


# ---------------------------------------------------------------------------
# Path (c): free-text with no PR URL and no diff -> honest guidance (Fix B)
# ---------------------------------------------------------------------------
class TestPathFreeText:
    @pytest.mark.asyncio
    async def test_free_text_returns_guidance_not_internal_error(self, monkeypatch, restore_settings):
        """A context-free free-text ask must NOT call PRQuestions/PRAgent and must NOT
        return the internal-error fallback; it returns honest guidance instead."""
        pr_questions_used = {"called": False}
        pr_agent_used = {"called": False}

        class FakePRQuestions:
            def __init__(self, pr_url, args=None, ai_handler=None):
                pr_questions_used["called"] = True
                self.prediction = "SHOULD NOT BE USED"

            async def run(self):
                return ""

        async def fail_handle_request(self, *a, **k):
            pr_agent_used["called"] = True
            return True

        monkeypatch.setattr("pr_agent.tools.pr_questions.PRQuestions", FakePRQuestions)
        from pr_agent.agent.pr_agent import PRAgent
        monkeypatch.setattr(PRAgent, "handle_request", fail_handle_request)

        out = await route_and_run("what does this codebase do?")
        assert out == "PR-Agent requires a PR URL or a supplied diff."
        assert out != _error_fallback("ask")
        assert out != _error_fallback("request")
        assert pr_questions_used["called"] is False
        assert pr_agent_used["called"] is False

    @pytest.mark.asyncio
    async def test_free_text_without_question_mark_also_guidance(self, monkeypatch, restore_settings):
        # The verb heuristic routes interrogative openers to 'ask'; still no URL/diff -> (c).
        out = await route_and_run("what is up")
        assert out == "PR-Agent requires a PR URL or a supplied diff."


# ---------------------------------------------------------------------------
# defensive capture / fallbacks
# ---------------------------------------------------------------------------
class TestDefensiveCapture:
    @pytest.mark.asyncio
    async def test_handle_request_false_returns_error_fallback(self, monkeypatch, restore_settings):
        async def fake_handle_request(self, pr_url, request, notify=None):
            return False  # swallowed internal failure

        from pr_agent.agent.pr_agent import PRAgent
        monkeypatch.setattr(PRAgent, "handle_request", fake_handle_request)

        out = await route_and_run(f"review {PR_URL}")
        assert out == _error_fallback("review")

    @pytest.mark.asyncio
    async def test_ok_but_no_artifact_returns_empty_fallback(self, monkeypatch, restore_settings):
        async def fake_handle_request(self, pr_url, request, notify=None):
            # ok=True but never sets data["artifact"] (early-return paths)
            return True

        from pr_agent.agent.pr_agent import PRAgent
        monkeypatch.setattr(PRAgent, "handle_request", fake_handle_request)
        # ensure no stale artifact from a prior test
        _clear_artifact()

        out = await route_and_run(f"review {PR_URL}")
        assert out == _empty_fallback("review")

    @pytest.mark.asyncio
    async def test_ask_that_raises_returns_error_fallback(self, monkeypatch, restore_settings):
        class RaisingPRQuestions:
            def __init__(self, pr_url, args=None, ai_handler=None):
                self.prediction = None

            async def run(self):
                raise RuntimeError("boom")

        monkeypatch.setattr("pr_agent.tools.pr_questions.PRQuestions", RaisingPRQuestions)
        # Use a PR URL so the ask path (a) actually runs PRQuestions (free-text no longer
        # invokes it after Fix B); a raise there -> error fallback.
        out = await route_and_run(f"what is the meaning of this? {PR_URL}")
        assert out == _error_fallback("ask")

    @pytest.mark.asyncio
    async def test_route_and_run_never_raises_on_garbage(self, restore_settings):
        # No monkeypatching: a host-less PR-agent run / ask should still return a string.
        for text in ("", None, "   ", "random text with no url and no diff"):
            out = await route_and_run(text)
            assert isinstance(out, str)


# ---------------------------------------------------------------------------
# Regression: production default must NEVER publish to the real PR
# ---------------------------------------------------------------------------
class TestPublishOutputForced:
    """Prove that _run_pr_agent and _run_ask force publish_output=False regardless
    of the global CONFIG.PUBLISH_OUTPUT default (which is True in production).

    Regression guards: these must fail if the no-publish overrides are ever dropped."""

    @pytest.mark.asyncio
    async def test_run_pr_agent_injects_no_publish_flags(self, monkeypatch, restore_settings):
        """_run_pr_agent must pass --config.publish_output=false and
        --config.publish_output_progress=false in the handle_request args list so that
        the tool writes into data['artifact'] rather than publishing to the real PR."""
        captured_args = {}

        async def fake_handle_request(self, pr_url, request, notify=None):
            captured_args["request"] = list(request)
            _set_artifact("REVIEW OUTPUT")
            return True

        from pr_agent.agent.pr_agent import PRAgent
        from pr_agent.mosaico.dispatch import _run_pr_agent
        monkeypatch.setattr(PRAgent, "handle_request", fake_handle_request)

        # Ensure the production default (publish_output=True) is in effect so
        # the test would fail if the flags were absent.
        global_settings.set("CONFIG.PUBLISH_OUTPUT", True)
        out = await _run_pr_agent(PR_URL, "review")

        assert out == "REVIEW OUTPUT"
        assert "--config.publish_output=false" in captured_args["request"], (
            "Production path must inject --config.publish_output=false; "
            "without it the tool publishes to the real PR and returns nothing to MOSAICO."
        )
        assert "--config.publish_output_progress=false" in captured_args["request"], (
            "Production path must inject --config.publish_output_progress=false."
        )

    @pytest.mark.asyncio
    async def test_run_ask_forces_publish_output_false(self, monkeypatch, restore_settings):
        """_run_ask must set CONFIG.PUBLISH_OUTPUT=False before calling PRQuestions.run()
        so that the publish guards in run() never publish_comment to the real PR.

        PRQuestions.parse_args() does a plain join (no --config.* parsing), so the
        arg-injection trick used by _run_pr_agent cannot apply; a settings.set() is
        required instead."""
        publish_output_at_run_time = {}

        class CapturingPRQuestions:
            def __init__(self, pr_url, args=None, ai_handler=None):
                self.prediction = "CAPTURED ANSWER"

            async def run(self):
                # Capture what get_settings() reports at the moment run() executes —
                # this is the value run()'s publish guards will read.
                publish_output_at_run_time["value"] = global_settings.get(
                    "CONFIG.PUBLISH_OUTPUT", True
                )

        monkeypatch.setattr("pr_agent.tools.pr_questions.PRQuestions", CapturingPRQuestions)

        from pr_agent.mosaico.dispatch import _run_ask
        # Force global default to True (production default) so the test would fail
        # if _run_ask does NOT explicitly override it.
        global_settings.set("CONFIG.PUBLISH_OUTPUT", True)
        out = await _run_ask(PR_URL, "what does this change?")

        assert out == "CAPTURED ANSWER"
        assert publish_output_at_run_time.get("value") is False, (
            "CONFIG.PUBLISH_OUTPUT must be False when PRQuestions.run() is called; "
            "without this, run()'s publish guards post comments to the real PR."
        )
