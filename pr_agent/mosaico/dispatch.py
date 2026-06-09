"""Text router: turn inbound MOSAICO text into a pr-agent command and return the
rendered markdown.

Three paths:
  (a) a host PR URL  -> run the verb via the host provider (URL drives provider).
  (b) a supplied unified diff -> set MOSAICO.INPUT + CONFIG.GIT_PROVIDER="mosaico_diff"
      on the context settings, run the verb via DiffInputProvider.
  (c) free-text with no PR URL and no diff -> honest guidance (ask needs a PR/diff).

Capture is DEFENSIVE everywhere: get_settings().get("data", {}).get("artifact", "")
(several tool paths never set it, and handle_request swallows exceptions -> False).
route_and_run NEVER raises; on failure/empty it returns an honest fallback string."""
import re

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger
from pr_agent.mosaico.diff_provider import parse_unified_diff

_VALID_VERBS = ("review", "improve", "describe", "ask")
_DEFAULT_VERB = "review"

# PR-URL detection: github/gitlab/bitbucket/azure-style hosts with a PR/MR path.
_PR_URL_RE = re.compile(
    r"https?://\S*?/(?:pull|pulls|merge_requests|pullrequest|pull-requests|_git/\S+/pullrequest)/\d+",
    re.IGNORECASE,
)

# Diff detection: a ```diff fence or a raw unified-diff header.
_DIFF_FENCE_RE = re.compile(r"```\s*diff", re.IGNORECASE)
_DIFF_HEADER_RE = re.compile(r"^diff --git ", re.MULTILINE)
_UNIFIED_HUNK_RE = re.compile(r"^@@ .* @@", re.MULTILINE)


def _detect_verb(text: str) -> str:
    """Pick a verb from the text. Defaults to 'review'. 'ask' wins when the text reads
    like a question and no other explicit verb is present."""
    low = (text or "").lower()
    # explicit slash command takes precedence
    for verb in _VALID_VERBS:
        if re.search(rf"(^|\s)/?{verb}\b", low):
            return verb
    # heuristic: a question mark or interrogative opener -> ask
    if "?" in low or re.match(r"\s*(what|why|how|when|where|who|which|is|are|does|do|can|should)\b", low):
        return "ask"
    return _DEFAULT_VERB


def _find_pr_url(text: str):
    m = _PR_URL_RE.search(text or "")
    if m:
        return m.group(0)
    return None


def _looks_like_diff(text: str) -> bool:
    if not text:
        return False
    return bool(_DIFF_FENCE_RE.search(text) or _DIFF_HEADER_RE.search(text) or _UNIFIED_HUNK_RE.search(text))


def _extract_diff(text: str) -> str:
    """Return the unified-diff body, unwrapping a ```diff fence if present."""
    fence = re.search(r"```\s*diff\s*\n(.*?)```", text, re.IGNORECASE | re.DOTALL)
    if fence:
        return fence.group(1)
    return text


def _diff_prose(text: str) -> str:
    """The natural-language prose around a supplied diff, used for verb detection so
    punctuation inside the patch body ('?' in a ternary/regex/comment) does not flip the
    default 'review' into 'ask'. A genuine question in the surrounding prose (e.g.
    'what changed here?') is preserved."""
    # Drop a fenced ```diff ... ``` block entirely.
    without_fence = re.sub(r"```\s*diff\s*\n.*?```", " ", text, flags=re.IGNORECASE | re.DOTALL)
    if without_fence != text:
        return without_fence
    # Raw (unfenced) diff: keep only the text before the first diff/hunk header.
    m = re.search(r"^(?:diff --git |@@ )", text, re.MULTILINE)
    return text[:m.start()] if m else text


def _capture_artifact() -> str:
    data = get_settings().get("data", {}) or {}
    return (data.get("artifact", "") or "").strip()


def _empty_fallback(verb: str) -> str:
    return f"PR-Agent {verb}: no output produced (e.g. no files/changes detected)."


def _error_fallback(verb: str) -> str:
    return f"PR-Agent could not complete the {verb} (internal error; see agent logs)."


def _ask_needs_context_fallback() -> str:
    """Honest guidance for a context-free input (no PR URL, no diff). Every verb needs a
    PR/diff to act on, so we return guidance rather than invoking a tool that would fail."""
    return "PR-Agent requires a PR URL or a supplied diff."


async def _run_pr_agent(target: str, verb: str) -> str:
    """Run a review/improve/describe verb via PRAgent.handle_request, defensively.
    Force non-publishing output capture: the tools render into get_settings().data only
    when publish_output is False; with the default True they'd publish to the real PR and
    return nothing to MOSAICO."""
    from pr_agent.agent.pr_agent import PRAgent
    ok = await PRAgent().handle_request(
        target,
        ["/" + verb, "--config.publish_output=false", "--config.publish_output_progress=false"],
    )
    if ok is False:
        return _error_fallback(verb)
    artifact = _capture_artifact()
    return artifact if artifact else _empty_fallback(verb)


async def _run_ask(target: str, question: str) -> str:
    """Run the ask path directly via PRQuestions (it uses get_git_provider()(pr_url),
    not the with-context variant). PRQuestions.run() is NOT wrapped by handle_request's
    try/except, so wrap it here and treat an exception like a swallowed failure.

    PRQuestions.parse_args() joins args as plain text (no --config.* parsing), so the
    arg-injection trick used by _run_pr_agent cannot apply here. Instead, force
    publish_output=False on the per-request settings copy (executor.py deepcopies
    global_settings into starlette_context, so this write is request-scoped) before
    constructing PRQuestions — run() reads config.publish_output with no
    apply_repo_settings call after this point that could re-enable publishing."""
    from pr_agent.tools.pr_questions import PRQuestions
    get_settings().set("CONFIG.PUBLISH_OUTPUT", False)
    get_settings().set("CONFIG.PUBLISH_OUTPUT_PROGRESS", False)
    try:
        q = PRQuestions(target, args=[question])
        await q.run()
    except Exception:
        get_logger().exception("MOSAICO: ask path failed")
        return _error_fallback("ask")
    answer = (q.prediction or "").strip()
    return answer if answer else _empty_fallback("ask")


def _simple_languages(files) -> dict:
    """Best-effort language map (extension -> count) for get_main_pr_language; tolerant
    of empties (downstream handles an empty dict)."""
    langs = {}
    for f in files:
        name = getattr(f, "filename", "") or ""
        if "." in name:
            ext = name.rsplit(".", 1)[1].lower()
            langs[ext] = langs.get(ext, 0) + 1
    return langs


async def route_and_run(user_text: str) -> str:
    """Route inbound text to a pr-agent command and return rendered markdown. Never raises."""
    try:
        text = user_text or ""
        verb = _detect_verb(text)

        # Path (a): a host PR URL — run via the host provider (URL drives provider).
        pr_url = _find_pr_url(text)
        if pr_url:
            if verb == "ask":
                return await _run_ask(pr_url, text)
            return await _run_pr_agent(pr_url, verb)

        # Path (b): a supplied unified diff.
        if _looks_like_diff(text):
            diff_body = _extract_diff(text)
            # Detect the verb from the prose only: a '?' in the patch body must not flip review to ask.
            verb = _detect_verb(_diff_prose(text))
            parsed = parse_unified_diff(diff_body)
            if not parsed:
                return _empty_fallback(verb)
            settings = get_settings()
            settings.set("MOSAICO.INPUT", {
                "files": parsed,
                "languages": _simple_languages(parsed),
                "title": "Supplied diff",
            })
            settings.set("CONFIG.GIT_PROVIDER", "mosaico_diff")
            if verb == "ask":
                return await _run_ask("mosaico://supplied-diff", text)
            return await _run_pr_agent("mosaico://supplied-diff", verb)

        # Path (c): free-text with no PR URL and no supplied diff. PRQuestions needs a
        # diff/PR to answer, so return honest guidance rather than a false internal error.
        return _ask_needs_context_fallback()
    except Exception:
        get_logger().exception("MOSAICO: route_and_run failed")
        return _error_fallback("request")
