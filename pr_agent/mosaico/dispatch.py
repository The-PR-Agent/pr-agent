"""Text router: turn inbound MOSAICO text into a pr-agent command and return the
rendered markdown.

Three paths:
  (a) a host PR URL  -> fetch the public unified diff by appending '.diff', then
      route through the token-free mosaico_diff provider.  Fails honestly if the
      repo is private / host unsupported.
  (b) a supplied unified diff -> set MOSAICO.INPUT + CONFIG.GIT_PROVIDER="mosaico_diff"
      on the context settings, run the verb via DiffInputProvider.
  (c) free-text with no PR URL and no diff -> honest guidance (ask needs a PR/diff).

Capture is DEFENSIVE everywhere: get_settings().get("data", {}).get("artifact", "")
(several tool paths never set it, and handle_request swallows exceptions -> False).
route_and_run NEVER raises; on failure/empty it returns an honest fallback string."""
import asyncio
import ipaddress
import re
import socket
from typing import NamedTuple, Optional
from urllib.parse import urljoin, urlparse

import aiohttp

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger
from pr_agent.mosaico.diff_provider import parse_unified_diff

_VALID_VERBS = ("review", "improve", "describe", "ask")
_DEFAULT_VERB = "review"

_DIFF_FETCH_TIMEOUT_S = 20
_DIFF_FETCH_MAX_BYTES = 4_000_000  # ~4 MB; larger diffs exceed model context anyway
_DIFF_FETCH_MAX_REDIRECTS = 5

# PR-URL detection: github/gitlab/bitbucket/azure-style hosts with a PR/MR path.
_PR_URL_RE = re.compile(
    r"https?://\S*?/(?:pull|pulls|merge_requests|pullrequest|pull-requests|_git/\S+/pullrequest)/\d+",
    re.IGNORECASE,
)

# Diff detection: a ```diff fence or a raw unified-diff header.
_DIFF_FENCE_RE = re.compile(r"```\s*diff", re.IGNORECASE)
_DIFF_HEADER_RE = re.compile(r"^diff --git ", re.MULTILINE)
_UNIFIED_HUNK_RE = re.compile(r"^@@ .* @@", re.MULTILINE)


class RouteResult(NamedTuple):
    """Routing outcome: rendered text + whether it succeeded (drives A2A complete vs failed)."""
    text: str
    ok: bool


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


def _ip_is_blocked(addr) -> bool:
    """Reject non-public IP ranges (SSRF guard): private/loopback/link-local (incl. cloud
    metadata 169.254.0.0/16), reserved, multicast, unspecified."""
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


async def _host_resolves_public(host: str) -> bool:
    """True only if `host` resolves and EVERY resolved IP is public. DNS runs in a thread
    so it does not block the event loop. Any failure -> False (fail closed)."""
    if not host:
        return False
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except Exception:
        return False
    saw = False
    for info in infos:
        ip = info[4][0].split("%")[0]  # strip IPv6 zone id
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        saw = True
        if _ip_is_blocked(addr):
            return False
    return saw


async def _url_is_safe(url: str) -> bool:
    """SSRF gate for one URL: https scheme + a hostname that resolves only to public IPs."""
    try:
        u = urlparse(url)
    except Exception:
        return False
    if u.scheme != "https" or not u.hostname:
        return False
    return await _host_resolves_public(u.hostname)


async def _fetch_public_diff(pr_url: str) -> Optional[str]:
    """Fetch the public unified diff for a GitHub/GitLab PR/MR URL by appending '.diff'.
    Returns the diff text, or None on any failure. No auth - public repos only. SSRF-guarded:
    https-only and the host (and every redirect hop) must resolve to public IPs; degrades to
    None so the caller fails honestly."""
    diff_url = pr_url + ".diff"
    headers = {"User-Agent": "pr-agent-mosaico"}
    try:
        timeout = aiohttp.ClientTimeout(total=_DIFF_FETCH_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            url = diff_url
            for _ in range(_DIFF_FETCH_MAX_REDIRECTS + 1):
                if not await _url_is_safe(url):
                    get_logger().info(f"MOSAICO: diff fetch blocked unsafe/non-public URL: {url}")
                    return None
                async with session.get(url, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("Location")
                        if not loc:
                            return None
                        url = urljoin(url, loc)
                        continue
                    if resp.status != 200:
                        get_logger().info(f"MOSAICO: diff fetch {url} -> HTTP {resp.status}")
                        return None
                    # StreamReader.read(n) returns only buffered bytes; drain in chunks with a cap.
                    chunks = []
                    total = 0
                    async for chunk in resp.content.iter_chunked(65536):
                        total += len(chunk)
                        if total > _DIFF_FETCH_MAX_BYTES:
                            get_logger().info(f"MOSAICO: diff fetch {url} exceeds size cap; skipping.")
                            return None
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    text = raw.decode("utf-8", errors="replace")
                    return text if text.strip() else None
            get_logger().info(f"MOSAICO: diff fetch exceeded redirect limit: {diff_url}")
            return None
    except Exception as e:
        get_logger().info(f"MOSAICO: diff fetch failed for {diff_url}: {e}")
        return None


def _pr_fetch_failed_fallback(pr_url: str) -> str:
    return (f"PR-Agent could not fetch a public diff for {pr_url} "
            f"(private repo, unsupported host such as Azure DevOps/Bitbucket, "
            f"or the host blocked the request). "
            f"Paste the unified diff directly, or supply a git access token.")


async def _run_pr_agent(target: str, verb: str) -> "RouteResult":
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
        return RouteResult(_error_fallback(verb), ok=False)
    artifact = _capture_artifact()
    return RouteResult(artifact, ok=True) if artifact else RouteResult(_empty_fallback(verb), ok=True)


async def _run_ask(target: str, question: str) -> "RouteResult":
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
        return RouteResult(_error_fallback("ask"), ok=False)
    answer = (q.prediction or "").strip()
    return RouteResult(answer, ok=True) if answer else RouteResult(_empty_fallback("ask"), ok=True)


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


async def _run_on_diff(diff_body: str, verb: str, text: str, title: str, empty_ok: bool = True) -> "RouteResult":
    """Parse a unified diff, install it as MOSAICO.INPUT under the mosaico_diff provider,
    and run the verb (token-free). Empty parse -> empty fallback (ok=True) when empty_ok is
    True (supplied-diff path); failure (ok=False) when empty_ok is False (PR-URL path, where
    an empty parse indicates the fetched body was not a real diff)."""
    parsed = parse_unified_diff(diff_body)
    if not parsed:
        if empty_ok:
            return RouteResult(_empty_fallback(verb), ok=True)
        return RouteResult(_pr_fetch_failed_fallback(title), ok=False)
    settings = get_settings()
    settings.set("MOSAICO.INPUT", {
        "files": parsed,
        "languages": _simple_languages(parsed),
        "title": title,
    })
    settings.set("CONFIG.GIT_PROVIDER", "mosaico_diff")
    if verb == "ask":
        return await _run_ask("mosaico://supplied-diff", text)
    return await _run_pr_agent("mosaico://supplied-diff", verb)


async def route_and_run_result(user_text: str) -> "RouteResult":
    """Route inbound text to a pr-agent command and return a RouteResult. Never raises."""
    try:
        text = user_text or ""
        verb = _detect_verb(text)

        # Path (a): a host PR URL — fetch the public unified diff and route through
        # the token-free mosaico_diff provider.
        pr_url = _find_pr_url(text)
        if pr_url:
            diff_body = await _fetch_public_diff(pr_url)
            if not diff_body:
                return RouteResult(_pr_fetch_failed_fallback(pr_url), ok=False)
            return await _run_on_diff(diff_body, verb, text, title=pr_url, empty_ok=False)

        # Path (b): a supplied unified diff.
        if _looks_like_diff(text):
            # Detect the verb from the prose only: a '?' in the patch body must not flip review to ask.
            verb = _detect_verb(_diff_prose(text))
            return await _run_on_diff(_extract_diff(text), verb, text, title="Supplied diff")

        # Path (c): free-text with no PR URL and no supplied diff. PRQuestions needs a
        # diff/PR to answer, so return honest guidance rather than a false internal error.
        return RouteResult(_ask_needs_context_fallback(), ok=True)
    except Exception:
        get_logger().exception("MOSAICO: route_and_run_result failed")
        return RouteResult(_error_fallback("request"), ok=False)


async def route_and_run(user_text: str) -> str:
    """Back-compat string wrapper around route_and_run_result (preserves existing callers/tests)."""
    return (await route_and_run_result(user_text)).text
