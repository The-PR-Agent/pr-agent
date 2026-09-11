import pytest

from pr_agent.algo.finding_verifier import (
    HEDGE_RE,
    build_verification_context,
    parse_verdict,
    referenced_pr_files,
    verify_findings,
)

HEDGED = {
    "relevant_file": "lib/src/profile/profile_providers.dart",
    "start_line": 116,
    "end_line": 120,
    "issue_header": "Duplicate History",
    "issue_content": "Unless the first Game Over is suppressed elsewhere, Recent Runs will show two entries.",
}
CONFIDENT = {
    "relevant_file": "lib/src/game/piece_tray.dart",
    "start_line": 400,
    "end_line": 401,
    "issue_header": "Possible Issue",
    "issue_content": "If drop resolution in `board_view.dart` still depends on `grip`, drags will land offset.",
}


def test_hedge_regex_flags_both_phrasings():
    assert HEDGE_RE.search(HEDGED["issue_content"])
    assert HEDGE_RE.search(CONFIDENT["issue_content"])
    assert not HEDGE_RE.search("The list is indexed past its length on line 4.")


def test_referenced_pr_files_by_basename():
    files = ["lib/src/game/board_view.dart", "lib/src/game/piece_tray.dart", "lib/x.dart"]
    assert referenced_pr_files(CONFIDENT, files) == ["lib/src/game/board_view.dart"]


def test_context_includes_own_file_and_referenced_files_within_budget():
    ctx, truncated = build_verification_context(
        CONFIDENT, "OWN" * 10, {"lib/src/game/board_view.dart": "OTHER" * 10}, max_chars=200
    )
    assert "piece_tray.dart" in ctx and "board_view.dart" in ctx
    assert len(ctx) <= 200 + 100  # headers allowed beyond budget only
    assert truncated is False


def test_context_reports_truncation_when_file_exceeds_budget():
    ctx, truncated = build_verification_context(
        CONFIDENT, "X" * 5000, {}, max_chars=500
    )
    assert truncated is True
    assert "... [truncated]" in ctx
    full, not_truncated = build_verification_context(CONFIDENT, "small", {}, max_chars=200000)
    assert not_truncated is False
    assert "small" in full


@pytest.mark.parametrize(
    "text,status",
    [
        ('{"status": "refuted", "evidence": "runs[0] = entry;", "reason": "same seed replaces"}', "refuted"),
        ('```json\n{"status":"confirmed","evidence":"x","reason":"y"}\n```', "confirmed"),
        ("garbage", "unverified"),
    ],
)
def test_parse_verdict(text, status):
    assert parse_verdict(text).status == status


@pytest.mark.parametrize(
    "text",
    [
        '{"status":"refuted","evidence":"","reason":"x"}',
        '{"status":"confirmed","evidence":"   ","reason":"y"}',
        '{"status":"refuted","evidence":null,"reason":"z"}',
        '{"status":"refuted","evidence":42,"reason":"z"}',
        '{"status":"confirmed","reason":"no evidence key"}',
    ],
)
def test_parse_verdict_requires_evidence_for_confirmed_or_refuted(text):
    verdict = parse_verdict(text)
    assert verdict.status == "unverified"
    assert verdict.reason == "no evidence quoted"


@pytest.mark.asyncio
async def test_verify_findings_drops_refuted_keeps_others():
    async def fetch(path):
        return f"content of {path}"

    async def call_model(system, user, files):
        assert isinstance(files, list)
        if "Duplicate History" in user:
            return '{"status":"refuted","evidence":"runs[0] = entry;","reason":"replaces same seed"}'
        return '{"status":"unverified","evidence":"","reason":"not enough context"}'

    results = await verify_findings(
        [HEDGED, CONFIDENT], fetch, ["lib/src/game/board_view.dart"], call_model, max_findings=10
    )
    statuses = {r[0]["issue_header"]: r[1].status for r in results}
    assert statuses == {"Duplicate History": "refuted", "Possible Issue": "unverified"}


@pytest.mark.asyncio
async def test_verify_respects_max_findings():
    async def fetch(path):
        return ""

    calls = []

    async def call_model(system, user, files):
        calls.append(1)
        return '{"status":"confirmed","evidence":"e","reason":"r"}'

    results = await verify_findings(
        [HEDGED, CONFIDENT, dict(HEDGED)], fetch, [], call_model, max_findings=2
    )
    assert len(calls) == 2 and len(results) == 3
    assert results[2][1].status == "unverified"


@pytest.mark.asyncio
async def test_verify_downgrades_refuted_when_context_truncated():
    async def fetch(path):
        return "Y" * 10000

    async def call_model(system, user, files):
        return '{"status":"refuted","evidence":"line","reason":"no"}'

    results = await verify_findings(
        [HEDGED], fetch, [], call_model, max_findings=10, max_chars=400
    )
    assert results[0][1].status == "unverified"
    assert results[0][1].reason == "context truncated"


@pytest.mark.asyncio
async def test_verify_keeps_refuted_when_context_is_complete():
    async def fetch(path):
        return "short"

    async def call_model(system, user, files):
        return '{"status":"refuted","evidence":"line","reason":"no"}'

    results = await verify_findings(
        [HEDGED], fetch, [], call_model, max_findings=10, max_chars=200000
    )
    assert results[0][1].status == "refuted"


@pytest.mark.asyncio
async def test_verify_type_error_from_model_becomes_unverified():
    async def fetch(path):
        return ""

    async def call_model(system, user, files):
        raise TypeError("unexpected kwargs")

    results = await verify_findings([HEDGED], fetch, [], call_model, max_findings=10)
    assert results[0][1].status == "unverified"
    assert results[0][1].reason == "verifier error: TypeError"


@pytest.mark.asyncio
async def test_verify_caches_fetched_file_content_per_pass():
    fetches = []

    async def fetch(path):
        fetches.append(path)
        return f"content:{path}"

    async def call_model(system, user, files):
        return '{"status":"confirmed","evidence":"e","reason":"r"}'

    issue_a = dict(HEDGED)
    issue_b = dict(HEDGED)
    issue_b["issue_header"] = "Second"
    await verify_findings([issue_a, issue_b], fetch, [], call_model, max_findings=10)
    own = HEDGED["relevant_file"]
    assert fetches.count(own) == 1
