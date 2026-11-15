"""LLM judges for the review-quality eval: catch (did the review raise a known finding) and precision (is each
posted finding a real defect, checked against the code at the reviewed commit with read-only lookups)."""
import json
import os
import pathlib
import subprocess
import time

import litellm

KEY_ENV = {"openai": ("OPENAI__KEY", "OPENAI_KEY", "OPENAI_API_KEY")}
MAX_LOOKUPS, READ_LINES, GREP_LINES, MAX_TEXT = 8, 120, 40, 200

CATCH_SYSTEM = """You judge whether an automated code review raised known problems. Each known finding is a real \
issue a human maintainer raised on this commit. For each known finding answer: caught (the review clearly raises \
the same problem, pointing at the relevant code), partial (same area or risk but misses the point), or missed. Be \
strict: vague advice does not catch a specific issue, and a generic risk header catches nothing."""

CATCH_USER = """Known findings:
{known}

The review:
<<<REVIEW
{review}
REVIEW>>>

Reply with JSON only: {{"verdicts": [{{"label": "<label>", "verdict": "caught|partial|missed"}}]}}"""

PRECISION_SYSTEM = """You judge findings an automated reviewer posted on a pull request. For each finding decide \
whether it is a real defect or risk in the code at the PR head that a maintainer should act on. Check claims \
against the code, not their wording. Speculative claims are valid only if you confirm the condition can happen; a \
finding is not valid if the code already guards or handles it, if it is style-only or purely theoretical, or if \
it asks the author to "check" something without showing a defect."""

PRECISION_A = """{context}

Findings (numbered):
{findings}

First list the code outside the diff you need to read to decide: callers, defaults, guards, tests. Reply with JSON \
only: {{"lookups": [{{"kind": "read", "path": "<repo path>", "start": <line>, "end": <line>}} or {{"kind": "grep", \
"text": "<exact identifier or string>"}}]}}, at most {max_lookups} lookups; [] if none are needed."""

PRECISION_B = """Lookup results at the PR head:
{results}

Reply with JSON only: {{"verdicts": [{{"id": <n>, "valid": "yes|no|unsure", "reason": "<one line>"}}]}}, one per \
finding."""


def api_key(model):
    provider = model.split("/", 1)[0] if "/" in model else "openai"
    for name in KEY_ENV.get(provider, ()):
        if os.environ.get(name):
            return os.environ[name]
    raise SystemExit(f"no API key in the environment for judge model {model!r}")


def warm_up(model):
    """Resolve the provider once on the main thread: litellm's lazy imports are not thread-safe, and two worker
    threads resolving at once failed with an ImportError inside get_llm_provider."""
    api_key(model)
    litellm.get_llm_provider(model)


def complete(model, system, messages, max_tokens=4096):
    """Text of one completion; retries rate limits and server errors."""
    retryable = (litellm.RateLimitError, litellm.InternalServerError, litellm.ServiceUnavailableError,
                 litellm.APIConnectionError, litellm.Timeout)
    for attempt in range(5):
        try:
            return _complete_once(model, system, messages, max_tokens)
        except retryable:
            time.sleep(10 * (attempt + 1))
    return _complete_once(model, system, messages, max_tokens)


def _complete_once(model, system, messages, max_tokens):
    r = litellm.completion(model=model, api_key=api_key(model), max_tokens=max_tokens,
                           messages=[{"role": "system", "content": system}, *messages])
    u = r.usage
    return r.choices[0].message.content or "", {"input": u.prompt_tokens, "output": u.completion_tokens}


def parse_json(text):
    a, b = text.find("{"), text.rfind("}")
    try:
        return json.loads(text[a:b + 1]) if 0 <= a < b else {}
    except json.JSONDecodeError:
        return {}


def judge_catch(model, known, review, reverse=False):
    """{label: verdict} for one review against its case's known findings."""
    order = list(reversed(known)) if reverse else known
    text = "\n".join(f"- [{k['label']}] {k['finding']}"
                     + (f"\n  Supporting facts: {k['fact_needed']}" if k.get("fact_needed") else "") for k in order)
    out, usage = complete(model, CATCH_SYSTEM, [{"role": "user", "content": CATCH_USER.format(known=text,
                                                                                                review=review)}])
    got = {v.get("label"): v.get("verdict") for v in parse_json(out).get("verdicts", []) if isinstance(v, dict)}
    return {k["label"]: got.get(k["label"], "missed") for k in known}, [usage]


def lookup(worktree, lk):
    """One read-only lookup inside the reviewed checkout; a malformed request from the model never aborts the eval."""
    try:
        return _lookup(worktree, lk)
    except Exception as e:  # noqa: BLE001  the judge's request is untrusted model output
        return f"invalid lookup ({type(e).__name__})"


def _lookup(worktree, lk):
    """A line range of a file, or a fixed-string git grep, inside the reviewed checkout."""
    root = pathlib.Path(worktree).resolve()
    if lk.get("kind") == "read" and isinstance(lk.get("path"), str):
        path = (root / lk["path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return f"read {lk['path']}: not found"
        lines = path.read_text(errors="replace").splitlines()
        try:
            a = max(int(lk.get("start") or 1), 1)
            b = min(int(lk.get("end") or a + READ_LINES), a + READ_LINES - 1, len(lines))
        except (TypeError, ValueError):
            return f"read {lk['path']}: bad line range"
        return f"read {lk['path']} [{a}-{b}]\n" + "\n".join(f"{n}: {lines[n - 1]}" for n in range(a, b + 1))
    if lk.get("kind") == "grep" and isinstance(lk.get("text"), str) and lk["text"].strip():
        r = subprocess.run(["git", "grep", "-n", "-F", "-e", lk["text"][:MAX_TEXT]], cwd=root, capture_output=True,
                           text=True, timeout=60)
        hits = r.stdout.splitlines()
        more = f"\n... {len(hits) - GREP_LINES} more" if len(hits) > GREP_LINES else ""
        return f"grep {lk['text'][:MAX_TEXT]!r}: {len(hits)} hits\n" + "\n".join(hits[:GREP_LINES]) + more
    return "invalid lookup"


def judge_precision(model, context, found, worktree):
    """[valid verdict per finding] for one review's findings, after one round of lookups."""
    text = "\n\n".join(f"[{i}] {t}\n{b}" for i, (t, b) in enumerate(found))
    msgs = [{"role": "user", "content": PRECISION_A.format(context=context, findings=text, max_lookups=MAX_LOOKUPS)}]
    out, u1 = complete(model, PRECISION_SYSTEM, msgs)
    lookups = [lk for lk in parse_json(out).get("lookups", []) if isinstance(lk, dict)][:MAX_LOOKUPS]
    results = "\n\n".join(lookup(worktree, lk) for lk in lookups) or "(no lookups)"
    msgs += [{"role": "assistant", "content": out}, {"role": "user", "content": PRECISION_B.format(results=results)}]
    out, u2 = complete(model, PRECISION_SYSTEM, msgs)
    got = {}
    for v in parse_json(out).get("verdicts", []):
        if isinstance(v, dict) and str(v.get("id", "")).isdigit():
            got[int(v["id"])] = str(v.get("valid", "unsure")).lower()
    return [got.get(i, "unsure") for i in range(len(found))], [u1, u2]
