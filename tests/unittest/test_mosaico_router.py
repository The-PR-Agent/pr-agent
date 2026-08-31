"""Tests for the MOSAICO dispatch router (capture/fallback behavior).

Pins: each path returns a string and NEVER raises; no-files/no-suggestions/empty-ask
-> empty-fallback string; a tool that raises (monkeypatched) -> error-fallback string
with no exception escaping.

asyncio_mode=auto."""
import aiohttp
import pytest

from pr_agent.config_loader import get_settings, global_settings
from pr_agent.mosaico import dispatch
from pr_agent.mosaico.dispatch import (
    _detect_verb,
    _diff_prose,
    _empty_fallback,
    _error_fallback,
    _explicit_verb,
    _split_turns,
    route_and_run,
    route_and_run_result,
)

PR_URL = "https://github.com/org/repo/pull/123"
DEAD_PR_URL = "https://github.com/org/repo/pull/999999999"
PRIVATE_PR_URL = "https://github.com/acme/private-repo/pull/7"

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

# Raw (unfenced) unified diff used for mocking _fetch_public_diff responses.
SAMPLE_RAW_DIFF = """diff --git a/foo.py b/foo.py
index 1111111..2222222 100644
--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,2 @@
-x = 1
+x = 2
 y = 3
"""

CORRECTED_DIFF = """```diff
diff --git a/bar.py b/bar.py
index 3333333..4444444 100644
--- a/bar.py
+++ b/bar.py
@@ -1,2 +1,2 @@
-a = 1
+a = 2
 b = 3
```"""

# Keep the 'Estimated effort to review:' line: r'review\b' does not match 'reviewer'.
AGENT_REVIEW_OUTPUT = """## PR Reviewer Guide 🔍

Here are some key observations to aid the review process:

### ⏱️ Estimated effort to review: 1 🔵⚪⚪⚪⚪

### 🧪 No relevant tests"""

AUTH_DIFF = """```diff
diff --git a/auth.py b/auth.py
index 1111111..2222222 100644
--- a/auth.py
+++ b/auth.py
@@ -1,3 +1,3 @@
 def check_password(user, pw):
-    return user.password == pw
+    return constant_time_compare(user.password_hash, hash_pw(pw))
```"""

BILLING_DIFF = """```diff
diff --git a/billing/invoice.py b/billing/invoice.py
index 3333333..4444444 100644
--- a/billing/invoice.py
+++ b/billing/invoice.py
@@ -1,3 +1,3 @@
 def total(items):
-    return sum(i.price for i in items)
+    return sum(i.price * i.quantity for i in items)
```"""

RAW_DIFF_BODY = """diff --git a/bar.py b/bar.py
index 3333333..4444444 100644
--- a/bar.py
+++ b/bar.py
@@ -1,2 +1,2 @@
-a = 1
+a = 2
 b = 3"""

_SENTINEL = object()


@pytest.fixture
def restore_settings():
    """Snapshot/restore the settings keys the router mutates, leaving global_settings
    exactly as found."""
    keys = ["CONFIG.GIT_PROVIDER", "CONFIG.PUBLISH_OUTPUT", "CONFIG.PUBLISH_OUTPUT_PROGRESS",
            "CONFIG.PROPAGATE_TOOL_ERRORS"]
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
# Path (a): host PR URL — now fetches diff and routes through mosaico_diff
# ---------------------------------------------------------------------------
class TestPathPrUrl:
    @pytest.mark.asyncio
    async def test_pr_url_runs_handle_request_and_returns_artifact(self, monkeypatch, restore_settings):
        captured = {}

        async def fake_fetch_public_diff(pr_url):
            return SAMPLE_RAW_DIFF

        async def fake_handle_request(self, pr_url, request, notify=None):
            captured["pr_url"] = pr_url
            captured["request"] = request
            _set_artifact("REVIEW MARKDOWN")
            return True

        monkeypatch.setattr(dispatch, "_fetch_public_diff", fake_fetch_public_diff)
        from pr_agent.agent.pr_agent import PRAgent
        monkeypatch.setattr(PRAgent, "handle_request", fake_handle_request)

        out = await route_and_run(f"review {PR_URL}")
        assert out == "REVIEW MARKDOWN"
        # After Fix B path (a) routes through the supplied-diff target, not the raw PR URL.
        assert captured["pr_url"] == "mosaico://supplied-diff"
        assert global_settings.get("CONFIG.GIT_PROVIDER") == "mosaico_diff"
        # _run_pr_agent must inject the no-publish flags.
        assert "--config.publish_output=false" in captured["request"]
        assert "--config.publish_output_progress=false" in captured["request"]
        assert "/review" in captured["request"]

    @pytest.mark.asyncio
    async def test_pr_url_routes_through_mosaico_diff(self, monkeypatch, restore_settings):
        """After Fix B, a PR URL must be routed through mosaico_diff (not the host provider)."""

        async def fake_fetch_public_diff(pr_url):
            return SAMPLE_RAW_DIFF

        async def fake_handle_request(self, pr_url, request, notify=None):
            _set_artifact("OK")
            return True

        monkeypatch.setattr(dispatch, "_fetch_public_diff", fake_fetch_public_diff)
        from pr_agent.agent.pr_agent import PRAgent
        monkeypatch.setattr(PRAgent, "handle_request", fake_handle_request)

        await route_and_run(f"review {PR_URL}")
        assert global_settings.get("CONFIG.GIT_PROVIDER") == "mosaico_diff"

    @pytest.mark.asyncio
    async def test_pr_url_non_diff_body_marks_failed(self, monkeypatch, restore_settings):
        """When _fetch_public_diff returns HTTP 200 but with non-diff content (e.g. an HTML
        login page), parse_unified_diff yields [] and _run_on_diff must report ok=False with
        the pr-fetch-failed fallback — NOT ok=True with the empty fallback."""

        async def fake_fetch_public_diff(pr_url):
            return "<html><body>Sign in</body></html>"

        monkeypatch.setattr(dispatch, "_fetch_public_diff", fake_fetch_public_diff)

        result = await route_and_run_result(f"review {PR_URL}")
        assert result.ok is False
        assert "could not fetch" in result.text


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

        async def fake_fetch_public_diff(pr_url):
            return SAMPLE_RAW_DIFF

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

        monkeypatch.setattr(dispatch, "_fetch_public_diff", fake_fetch_public_diff)
        monkeypatch.setattr("pr_agent.tools.pr_questions.PRQuestions", FakePRQuestions)
        from pr_agent.agent.pr_agent import PRAgent
        monkeypatch.setattr(PRAgent, "handle_request", fail_handle_request)

        out = await route_and_run(f"what does this change? {PR_URL}")
        assert out == "URL ANSWER"
        # After Fix B path (a) routes through supplied-diff target, not the raw PR URL.
        assert captured["pr_url"] == "mosaico://supplied-diff"
        assert global_settings.get("CONFIG.GIT_PROVIDER") == "mosaico_diff"
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
        async def fake_fetch_public_diff(pr_url):
            return SAMPLE_RAW_DIFF

        async def fake_handle_request(self, pr_url, request, notify=None):
            return False  # swallowed internal failure

        monkeypatch.setattr(dispatch, "_fetch_public_diff", fake_fetch_public_diff)
        from pr_agent.agent.pr_agent import PRAgent
        monkeypatch.setattr(PRAgent, "handle_request", fake_handle_request)

        out = await route_and_run(f"review {PR_URL}")
        assert out == _error_fallback("review")

    @pytest.mark.asyncio
    async def test_ok_but_no_artifact_returns_empty_fallback(self, monkeypatch, restore_settings):
        """Empty is a legitimate SUCCESS: /improve on a trivial diff yields no suggestions.
        Failure comes from the exception, never from emptiness. Mapping empty->ok=False fails
        ONLY this test, so it is the sole guard on that property — do not loosen it."""
        async def fake_fetch_public_diff(pr_url):
            return SAMPLE_RAW_DIFF

        async def fake_handle_request(self, pr_url, request, notify=None):
            # ok=True but never sets data["artifact"] (early-return paths)
            return True

        monkeypatch.setattr(dispatch, "_fetch_public_diff", fake_fetch_public_diff)
        from pr_agent.agent.pr_agent import PRAgent
        monkeypatch.setattr(PRAgent, "handle_request", fake_handle_request)
        # ensure no stale artifact from a prior test
        _clear_artifact()

        result = await route_and_run_result(f"review {PR_URL}")
        assert result.text == _empty_fallback("review")
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_tool_exception_marks_failed(self, monkeypatch, restore_settings):
        """The contrast to the test above: same empty artifact, but the tool raised. The
        re-raise makes handle_request return False, so the router must report ok=False
        rather than the empty fallback."""
        async def fake_fetch_public_diff(pr_url):
            return SAMPLE_RAW_DIFF

        async def raising_handle_request(self, pr_url, request, notify=None):
            raise RuntimeError("Failed to generate prediction with any model of ['m1', 'm2']")

        monkeypatch.setattr(dispatch, "_fetch_public_diff", fake_fetch_public_diff)
        from pr_agent.agent.pr_agent import PRAgent
        monkeypatch.setattr(PRAgent, "_handle_request", raising_handle_request)
        _clear_artifact()

        result = await route_and_run_result(f"review {PR_URL}")
        assert result.ok is False
        assert result.text == _error_fallback("review")
        assert "no output produced" not in result.text

    @pytest.mark.asyncio
    async def test_legacy_callers_unaffected_when_flag_off(self, monkeypatch, restore_settings):
        """Blast-radius guard for CLI/webhook callers, which pass no propagate_tool_errors
        arg: a tool failing internally must still swallow and still return True, unchanged."""
        import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_mod
        import pr_agent.mosaico.provider_registration  # noqa: F401
        from pr_agent.agent.pr_agent import PRAgent
        from pr_agent.mosaico.diff_provider import parse_unified_diff

        async def failing_chat_completion(self, model, system, user, temperature=0.2, **kwargs):
            raise RuntimeError("provider unavailable")

        monkeypatch.setattr(litellm_mod.LiteLLMAIHandler, "chat_completion", failing_chat_completion)
        global_settings.set("MOSAICO.INPUT", {"files": parse_unified_diff(SAMPLE_RAW_DIFF),
                                              "languages": {"py": 1}, "title": "Supplied diff"})
        global_settings.set("CONFIG.GIT_PROVIDER", "mosaico_diff")

        ok = await PRAgent().handle_request("mosaico://supplied-diff",
                                            ["/review", "--config.publish_output=false"])
        assert ok is True, "flag off must preserve the pre-existing swallow-and-succeed behaviour"

    @pytest.mark.asyncio
    async def test_propagate_tool_errors_passed_to_handle_request(self, monkeypatch, restore_settings):
        """Pins the wiring: without this arg the tools swallow and a failure reads as empty."""
        seen = {}

        async def fake_fetch_public_diff(pr_url):
            return SAMPLE_RAW_DIFF

        async def fake_handle_request(self, pr_url, request, notify=None):
            seen["args"] = list(request)
            global_settings.data = {"artifact": "OK"}
            return True

        monkeypatch.setattr(dispatch, "_fetch_public_diff", fake_fetch_public_diff)
        from pr_agent.agent.pr_agent import PRAgent
        monkeypatch.setattr(PRAgent, "handle_request", fake_handle_request)

        await route_and_run_result(f"review {PR_URL}")
        assert "--config.propagate_tool_errors=true" in seen["args"]

    @pytest.mark.asyncio
    async def test_propagate_flag_does_not_outlive_a_contextless_run(self, monkeypatch, restore_settings):
        """Runs the real arg-parsing path: the flag must be live at the tool run, and gone after.
        Contextless, get_settings() is global_settings, so without the restore one dispatch call
        would leave re-raising on for every later caller."""
        import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_mod
        import pr_agent.mosaico.provider_registration  # noqa: F401
        from pr_agent.mosaico.diff_provider import parse_unified_diff

        seen = {}

        async def failing_chat_completion(self, model, system, user, temperature=0.2, **kwargs):
            seen["during"] = get_settings().config.get("propagate_tool_errors")
            raise RuntimeError("provider unavailable")

        monkeypatch.setattr(litellm_mod.LiteLLMAIHandler, "chat_completion", failing_chat_completion)
        global_settings.set("MOSAICO.INPUT", {"files": parse_unified_diff(SAMPLE_RAW_DIFF),
                                              "languages": {"py": 1}, "title": "Supplied diff"})
        global_settings.set("CONFIG.GIT_PROVIDER", "mosaico_diff")
        global_settings.set("CONFIG.PROPAGATE_TOOL_ERRORS", False)

        result = await dispatch._run_pr_agent("mosaico://supplied-diff", "review")

        assert seen["during"] is True, "the arg never reached the tool run"
        assert result.ok is False, "the re-raise must surface as ok=False"
        assert global_settings.config.get("propagate_tool_errors") is False, "flag leaked past the run"

    @pytest.mark.asyncio
    async def test_ask_that_raises_returns_error_fallback(self, monkeypatch, restore_settings):
        async def fake_fetch_public_diff(pr_url):
            return SAMPLE_RAW_DIFF

        class RaisingPRQuestions:
            def __init__(self, pr_url, args=None, ai_handler=None):
                self.prediction = None

            async def run(self):
                raise RuntimeError("boom")

        monkeypatch.setattr(dispatch, "_fetch_public_diff", fake_fetch_public_diff)
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

    @pytest.mark.asyncio
    async def test_pr_url_fetch_failure_marks_failed(self, monkeypatch, restore_settings):
        """When _fetch_public_diff returns None, route_and_run_result must report ok=False
        and include the URL plus 'could not fetch' in the text."""

        async def fake_fetch_public_diff(pr_url):
            return None

        monkeypatch.setattr(dispatch, "_fetch_public_diff", fake_fetch_public_diff)

        result = await route_and_run_result(f"review {PR_URL}")
        assert result.ok is False
        assert PR_URL in result.text
        assert "could not fetch" in result.text


# ---------------------------------------------------------------------------
# _fetch_public_diff unit tests (no network — fake aiohttp.ClientSession)
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status, body, headers=None):
        self.status = status
        self.content = self
        self._body = body
        self.headers = headers if headers is not None else {}

    async def iter_chunked(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp

    def get(self, url, allow_redirects=True):
        return self._resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


async def _always_safe(url: str) -> bool:
    return True


class TestFetchPublicDiff:
    @pytest.mark.asyncio
    async def test_fetch_public_diff_non_200_returns_none(self, monkeypatch):
        resp = _FakeResp(404, b"")
        monkeypatch.setattr(dispatch, "_url_is_safe", _always_safe)
        monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _FakeSession(resp))
        result = await dispatch._fetch_public_diff("https://github.com/o/r/pull/1")
        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_public_diff_oversize_returns_none(self, monkeypatch):
        resp = _FakeResp(200, b"x" * (dispatch._DIFF_FETCH_MAX_BYTES + 1))
        monkeypatch.setattr(dispatch, "_url_is_safe", _always_safe)
        monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _FakeSession(resp))
        result = await dispatch._fetch_public_diff("https://github.com/o/r/pull/1")
        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_public_diff_assembles_multichunk_body(self, monkeypatch):
        # Body larger than the 65536 chunk size but under the cap: the full body must be
        # assembled across chunks, not truncated to the first read.
        body = b"a" * 200000
        resp = _FakeResp(200, body)
        monkeypatch.setattr(dispatch, "_url_is_safe", _always_safe)
        monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _FakeSession(resp))
        result = await dispatch._fetch_public_diff("https://github.com/o/r/pull/1")
        assert len(result) == 200000


# ---------------------------------------------------------------------------
# SSRF guard unit tests
# ---------------------------------------------------------------------------
class TestFetchPublicDiffSSRF:
    @pytest.mark.asyncio
    async def test_non_https_blocked(self):
        # http scheme is blocked before any DNS lookup
        assert await dispatch._url_is_safe("http://github.com/o/r/pull/1.diff") is False

    @pytest.mark.asyncio
    async def test_private_ip_blocked(self, monkeypatch):
        monkeypatch.setattr(dispatch.socket, "getaddrinfo",
                            lambda *a, **k: [(2, 1, 6, "", ("10.0.0.5", 0))])
        assert await dispatch._url_is_safe("https://internal.example/x/pull/1.diff") is False

    @pytest.mark.asyncio
    async def test_metadata_ip_blocked(self, monkeypatch):
        monkeypatch.setattr(dispatch.socket, "getaddrinfo",
                            lambda *a, **k: [(2, 1, 6, "", ("169.254.169.254", 0))])
        assert await dispatch._url_is_safe("https://metadata.example/pull/1.diff") is False

    @pytest.mark.asyncio
    async def test_public_ip_allowed(self, monkeypatch):
        # 140.82.121.4 is a public GitHub IP
        monkeypatch.setattr(dispatch.socket, "getaddrinfo",
                            lambda *a, **k: [(2, 1, 6, "", ("140.82.121.4", 0))])
        assert await dispatch._url_is_safe("https://github.com/o/r/pull/1.diff") is True

    @pytest.mark.asyncio
    async def test_fetch_blocks_unsafe_without_request(self, monkeypatch):
        # _url_is_safe returns False -> _fetch_public_diff must return None without calling GET
        async def always_unsafe(url: str) -> bool:
            return False

        class _NeverCalledSession:
            def get(self, url, allow_redirects=False):
                raise AssertionError("GET must not be called when URL is unsafe")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(dispatch, "_url_is_safe", always_unsafe)
        monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _NeverCalledSession())
        result = await dispatch._fetch_public_diff("https://169.254.169.254/x/pull/1")
        assert result is None

    @pytest.mark.asyncio
    async def test_redirect_to_internal_blocked(self, monkeypatch):
        """A redirect whose Location resolves to a private IP must be blocked."""
        PUBLIC_HOST = "github.com"
        PRIVATE_IP = "10.0.0.9"

        def fake_getaddrinfo(host, port, *a, **k):
            if host == PUBLIC_HOST:
                return [(2, 1, 6, "", ("140.82.121.4", 0))]
            # Any other host (incl. the raw IP string) -> private
            return [(2, 1, 6, "", (PRIVATE_IP, 0))]

        monkeypatch.setattr(dispatch.socket, "getaddrinfo", fake_getaddrinfo)

        # First GET returns a 302 pointing at an internal URL; second must never be reached.
        redirect_resp = _FakeResp(302, b"", headers={"Location": f"https://{PRIVATE_IP}/evil"})

        class _RedirectSession:
            def get(self, url, allow_redirects=False):
                return redirect_resp

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _RedirectSession())
        result = await dispatch._fetch_public_diff(f"https://{PUBLIC_HOST}/o/r/pull/1")
        assert result is None


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

        assert out.text == "REVIEW OUTPUT"
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

        assert out.text == "CAPTURED ANSWER"
        assert publish_output_at_run_time.get("value") is False, (
            "CONFIG.PUBLISH_OUTPUT must be False when PRQuestions.run() is called; "
            "without this, run()'s publish guards post comments to the real PR."
        )


def _blob(*turns) -> str:
    return "\n".join(f"{role}: {content}" for role, content in turns)


def _routed(monkeypatch):
    seen = {"fetched": []}

    async def fake_handle_request(self, pr_url, request, notify=None):
        seen["verb"] = next((a.lstrip("/") for a in request if a.startswith("/")), None)
        mosaico_input = global_settings.get("MOSAICO.INPUT") or {}
        seen["files"] = [f.filename for f in mosaico_input.get("files", [])]
        seen["title"] = mosaico_input.get("title")
        _set_artifact("ROUTED")
        return True

    class FakePRQuestions:
        def __init__(self, pr_url, args=None, ai_handler=None):
            seen["verb"] = "ask"
            seen["question"] = (args or [""])[0]
            mosaico_input = global_settings.get("MOSAICO.INPUT") or {}
            seen["files"] = [f.filename for f in mosaico_input.get("files", [])]
            seen["title"] = mosaico_input.get("title")
            self.prediction = "ROUTED"

        async def run(self):
            return ""

    async def fake_fetch_public_diff(pr_url):
        seen["fetched"].append(pr_url)
        return SAMPLE_RAW_DIFF if pr_url == PR_URL else None

    from pr_agent.agent.pr_agent import PRAgent
    monkeypatch.setattr(PRAgent, "handle_request", fake_handle_request)
    monkeypatch.setattr("pr_agent.tools.pr_questions.PRQuestions", FakePRQuestions)
    monkeypatch.setattr(dispatch, "_fetch_public_diff", fake_fetch_public_diff)
    return seen


class TestTurnSplitting:
    def test_splits_roles_and_keeps_multiline_content(self):
        turns = _split_turns(_blob(("user", "review this"),
                                   ("agent", AGENT_REVIEW_OUTPUT),
                                   ("user", "now describe it")))
        assert [t.role for t in turns] == ["user", "agent", "user"]
        assert [t.is_user for t in turns] == [True, False, True]
        assert turns[1].content == AGENT_REVIEW_OUTPUT
        assert turns[2].content == "now describe it"

    @pytest.mark.parametrize("text", [
        "review this PR",
        "user: alice\nplease review the config above",
        "here is my config\nuser: alice\nagent: bob\nreview it",
        "user: admin\npassword: hunter2\nreview this",
        "",
    ])
    def test_not_a_conversation_blob(self, text):
        assert _split_turns(text) == []

    def test_content_indentation_after_the_label_is_dropped(self):
        turns = _split_turns(_blob(("user", " diff --git a/x.py b/x.py"), ("agent", "ok")))
        assert turns[0].content == "diff --git a/x.py b/x.py"

    @pytest.mark.asyncio
    async def test_indented_raw_diff_in_a_turn_still_routes(self, monkeypatch, restore_settings):
        seen = _routed(monkeypatch)
        await route_and_run(_blob(("user", "here is a patch"),
                                  ("agent", "thanks"),
                                  ("user", f" {RAW_DIFF_BODY}\nnow describe it")))
        assert seen["verb"] == "describe"
        assert seen["files"] == ["bar.py"]


class TestConversationVerbRouting:
    @pytest.mark.asyncio
    async def test_review_history_does_not_stick_to_later_describe(self, monkeypatch, restore_settings):
        seen = _routed(monkeypatch)
        await route_and_run(_blob(("user", "review this"),
                                  ("agent", AGENT_REVIEW_OUTPUT),
                                  ("user", f"Now describe this diff\n{CORRECTED_DIFF}")))
        assert seen["verb"] == "describe"

    @pytest.mark.asyncio
    async def test_describe_history_does_not_stick_to_later_review(self, monkeypatch, restore_settings):
        seen = _routed(monkeypatch)
        await route_and_run(_blob(("user", "describe this"),
                                  ("agent", "### PR Type\nEnhancement"),
                                  ("user", f"now review it\n{CORRECTED_DIFF}")))
        assert seen["verb"] == "review"

    @pytest.mark.asyncio
    async def test_latest_turn_wins_over_earlier_user_verb(self, monkeypatch, restore_settings):
        seen = _routed(monkeypatch)
        await route_and_run(_blob(("user", "describe this"),
                                  ("agent", "### PR Type\nEnhancement"),
                                  ("user", f"now improve it\n{CORRECTED_DIFF}")))
        assert seen["verb"] == "improve"

    @pytest.mark.asyncio
    async def test_bare_diff_turn_falls_back_to_earlier_user_verb(self, monkeypatch, restore_settings):
        seen = _routed(monkeypatch)
        await route_and_run(_blob(("user", "describe this"),
                                  ("agent", AGENT_REVIEW_OUTPUT),
                                  ("user", CORRECTED_DIFF)))
        assert seen["verb"] == "describe"

    @pytest.mark.asyncio
    async def test_follow_up_question_routes_to_ask_with_latest_turn_only(self, monkeypatch, restore_settings):
        seen = _routed(monkeypatch)
        await route_and_run(_blob(("user", "review this"),
                                  ("agent", AGENT_REVIEW_OUTPUT),
                                  ("user", f"why is that a bug?\n{CORRECTED_DIFF}")))
        assert seen["verb"] == "ask"
        assert seen["question"].startswith("why is that a bug?")
        assert "PR Reviewer Guide" not in seen["question"]


class TestVerbNegationAndPosition:
    @pytest.mark.parametrize("text, expected", [
        ("Now describe it instead, do not review", "describe"),
        ("can you improve this? do not review", "improve"),
        ("do not review, describe this", "describe"),
        ("skip the review, just describe", "describe"),
        ("instead of reviewing, describe this", "describe"),
        ("describe this, then improve it", "describe"),
    ])
    def test_negated_and_positional_verbs(self, text, expected):
        assert _detect_verb(text) == expected

    @pytest.mark.parametrize("text, expected", [
        ("there is no bug, review this", "review"),
        ("i do not have time, review this", "review"),
        ("nothing to improve here?", "improve"),
        ("do not review", "review"),
    ])
    def test_negation_does_not_fire_on_ordinary_prose(self, text, expected):
        assert _detect_verb(text) == expected


class TestStickyReviewToken:
    def test_reviewer_heading_alone_does_not_reach_the_matcher(self):
        assert _explicit_verb("## PR Reviewer Guide 🔍") is None
        assert _detect_verb("## PR Reviewer Guide 🔍") == "review"

    def test_estimated_effort_line_is_the_token_that_matches(self):
        assert _explicit_verb("### ⏱️ Estimated effort to review: 1 🔵⚪⚪⚪⚪") == "review"

    @pytest.mark.asyncio
    async def test_real_review_output_in_history_does_not_capture_the_verb(self, monkeypatch, restore_settings):
        assert _explicit_verb(AGENT_REVIEW_OUTPUT) == "review", "guard: the constant must still be sticky"
        seen = _routed(monkeypatch)
        await route_and_run(_blob(("user", "review this"),
                                  ("agent", AGENT_REVIEW_OUTPUT),
                                  ("user", f"now describe it\n{CORRECTED_DIFF}")))
        assert seen["verb"] == "describe"


class TestProseAfterRawDiff:
    def test_prose_after_a_raw_diff_survives(self):
        prose = _diff_prose(f"here is the patch\n{RAW_DIFF_BODY}\nnow describe it please")
        assert "now describe it please" in prose
        assert "here is the patch" in prose
        assert "+a = 2" not in prose, "the patch body must still be excised"

    def test_prose_after_a_blank_separated_raw_diff_survives(self):
        prose = _diff_prose(f"{RAW_DIFF_BODY}\n\nnow describe it please")
        assert "now describe it please" in prose

    def test_fenced_diff_prose_is_unchanged(self):
        prose = _diff_prose(f"what changed here?\n{SAMPLE_DIFF}")
        assert "what changed here?" in prose
        assert "+x = 2" not in prose

    @pytest.mark.asyncio
    async def test_verb_written_after_a_raw_diff_is_honoured(self, monkeypatch, restore_settings):
        seen = _routed(monkeypatch)
        await route_and_run(f"{RAW_DIFF_BODY}\n\nnow describe it please")
        assert seen["verb"] == "describe"
        assert seen["files"] == ["bar.py"]

    @pytest.mark.asyncio
    async def test_verb_after_a_raw_diff_in_the_latest_turn_is_honoured(self, monkeypatch, restore_settings):
        seen = _routed(monkeypatch)
        await route_and_run(_blob(("user", "review this"),
                                  ("agent", AGENT_REVIEW_OUTPUT),
                                  ("user", f"{RAW_DIFF_BODY}\n\nnow describe it please")))
        assert seen["verb"] == "describe"
        assert seen["files"] == ["bar.py"]

    @pytest.mark.asyncio
    async def test_question_mark_inside_a_raw_patch_body_still_reviews(self, monkeypatch, restore_settings):
        seen = _routed(monkeypatch)
        raw_with_q = ("diff --git a/foo.py b/foo.py\n"
                      "@@ -1,2 +1,2 @@\n"
                      "-y = a if b else c\n"
                      "+y = a ? b : c  # is this right?\n"
                      " z = 3")
        await route_and_run(raw_with_q)
        assert seen["verb"] == "review"


class TestExtendedHeaderNotLeaked:
    RENAME_DIFF = ("diff --git a/a.py b/improve.py\n"
                   "similarity index 95%\n"
                   "rename from a.py\n"
                   "rename to improve.py\n"
                   "review this")

    NEW_FILE_DIFF = ("diff --git a/x.py b/x.py\n"
                     "new file mode 100644\n"
                     "index 0000000..1111111\n"
                     "--- /dev/null\n"
                     "+++ b/is_it_ok?.py\n"
                     "@@ -0,0 +1 @@\n"
                     "+x = 1")

    def test_rename_metadata_does_not_leak(self):
        assert _diff_prose(self.RENAME_DIFF).strip() == "review this"

    def test_new_file_metadata_does_not_leak(self):
        assert _diff_prose(self.NEW_FILE_DIFF).strip() == ""

    def test_renamed_path_does_not_override_the_request(self):
        assert _detect_verb(_diff_prose(self.RENAME_DIFF)) == "review"

    def test_question_mark_in_a_leaked_path_does_not_flip_to_ask(self):
        assert _detect_verb(_diff_prose(self.NEW_FILE_DIFF)) == "review"

    def test_prose_after_an_extended_header_patch_still_survives(self):
        prose = _diff_prose(f"{self.NEW_FILE_DIFF}\nnow describe it")
        assert prose.strip() == "now describe it"


class TestNewestContextWins:
    @pytest.mark.asyncio
    async def test_newest_diff_wins(self, monkeypatch, restore_settings):
        seen = _routed(monkeypatch)
        await route_and_run(_blob(("user", f"review this\n{SAMPLE_DIFF}"),
                                  ("agent", AGENT_REVIEW_OUTPUT),
                                  ("user", f"here is the corrected diff\n{CORRECTED_DIFF}")))
        assert seen["files"] == ["bar.py"], "the corrected diff must supersede the one it replaces"

    @pytest.mark.asyncio
    async def test_last_diff_wins_within_a_single_turn(self, monkeypatch, restore_settings):
        seen = _routed(monkeypatch)
        await route_and_run(f"review this\n{SAMPLE_DIFF}\nsorry, this one:\n{CORRECTED_DIFF}")
        assert seen["files"] == ["bar.py"]

    @pytest.mark.parametrize("stale_url", [DEAD_PR_URL, PRIVATE_PR_URL])
    @pytest.mark.asyncio
    async def test_fresh_diff_beats_stale_url_from_history(self, stale_url, monkeypatch, restore_settings):
        seen = _routed(monkeypatch)
        result = await route_and_run_result(
            _blob(("user", f"review {stale_url}"),
                  ("agent", AGENT_REVIEW_OUTPUT),
                  ("user", f"that URL is wrong, here is the diff instead\n{CORRECTED_DIFF}")))
        assert result.ok is True
        assert result.text == "ROUTED"
        assert seen["title"] == "Supplied diff"
        assert seen["files"] == ["bar.py"]
        assert seen["fetched"] == [], "the stale URL must never be fetched"

    @pytest.mark.asyncio
    async def test_answers_about_the_newest_patch_not_the_oldest(self, monkeypatch, restore_settings):
        seen = _routed(monkeypatch)
        result = await route_and_run_result(
            _blob(("user", f"review this auth change\n{AUTH_DIFF}"),
                  ("agent", AGENT_REVIEW_OUTPUT),
                  ("user", f"forget that, describe this new billing patch instead\n{BILLING_DIFF}")))
        assert result.ok is True
        assert seen["verb"] == "describe"
        assert seen["files"] == ["billing/invoice.py"]
        assert "auth.py" not in seen["files"], "answering about the superseded patch is silent wrongness"

    @pytest.mark.asyncio
    async def test_diff_from_an_agent_turn_is_still_usable(self, monkeypatch, restore_settings):
        seen = _routed(monkeypatch)
        await route_and_run(_blob(("user", "write me a patch"),
                                  ("agent", f"here you go\n{CORRECTED_DIFF}"),
                                  ("user", "review it")))
        assert seen["verb"] == "review"
        assert seen["files"] == ["bar.py"]

    @pytest.mark.asyncio
    async def test_current_turn_url_still_beats_older_diff(self, monkeypatch, restore_settings):
        seen = _routed(monkeypatch)
        await route_and_run(_blob(("user", f"review this\n{CORRECTED_DIFF}"),
                                  ("agent", AGENT_REVIEW_OUTPUT),
                                  ("user", f"actually use {PR_URL}")))
        assert seen["fetched"] == [PR_URL]
        assert seen["title"] == PR_URL
        assert seen["files"] == ["foo.py"]


class TestSingleTurnUnchanged:
    @pytest.mark.asyncio
    async def test_single_turn_diff_unchanged(self, monkeypatch, restore_settings):
        seen = _routed(monkeypatch)
        out = await route_and_run(f"review the following\n{SAMPLE_DIFF}")
        assert out == "ROUTED"
        assert seen["verb"] == "review"
        assert seen["title"] == "Supplied diff"
        assert seen["files"] == ["foo.py"]

    @pytest.mark.asyncio
    async def test_single_turn_pr_url_unchanged(self, monkeypatch, restore_settings):
        seen = _routed(monkeypatch)
        out = await route_and_run(f"describe {PR_URL}")
        assert out == "ROUTED"
        assert seen["verb"] == "describe"
        assert seen["fetched"] == [PR_URL]
        assert seen["title"] == PR_URL

    @pytest.mark.asyncio
    async def test_single_turn_unreachable_url_still_fails_honestly(self, monkeypatch, restore_settings):
        _routed(monkeypatch)
        result = await route_and_run_result(f"review {DEAD_PR_URL}")
        assert result.ok is False
        assert "could not fetch" in result.text

    @pytest.mark.asyncio
    async def test_conversation_with_no_context_returns_guidance(self, monkeypatch, restore_settings):
        _routed(monkeypatch)
        out = await route_and_run(_blob(("user", "hello"),
                                        ("agent", "hi, what can I do?"),
                                        ("user", "review my code please")))
        assert out == "PR-Agent requires a PR URL or a supplied diff."
