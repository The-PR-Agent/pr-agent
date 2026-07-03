"""Tests for the MOSAICO dispatch router (capture/fallback behavior).

Pins: each path returns a string and NEVER raises; no-files/no-suggestions/empty-ask
-> empty-fallback string; a tool that raises (monkeypatched) -> error-fallback string
with no exception escaping.

asyncio_mode=auto."""
import pytest

import aiohttp

from pr_agent.config_loader import global_settings
from pr_agent.mosaico import dispatch
from pr_agent.mosaico.dispatch import (_detect_verb, _empty_fallback,
                                       _error_fallback, route_and_run,
                                       route_and_run_result, RouteResult)

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

        out = await route_and_run(f"review {PR_URL}")
        assert out == _empty_fallback("review")

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
