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
    ctx = build_verification_context(
        CONFIDENT, "OWN" * 10, {"lib/src/game/board_view.dart": "OTHER" * 10}, max_chars=200
    )
    assert "piece_tray.dart" in ctx and "board_view.dart" in ctx
    assert len(ctx) <= 200 + 100  # headers allowed beyond budget only


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


@pytest.mark.asyncio
async def test_verify_findings_drops_refuted_keeps_others():
    async def fetch(path):
        return f"content of {path}"

    async def call_model(system, user):
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

    async def call_model(system, user):
        calls.append(1)
        return '{"status":"confirmed","evidence":"e","reason":"r"}'

    results = await verify_findings(
        [HEDGED, CONFIDENT, dict(HEDGED)], fetch, [], call_model, max_findings=2
    )
    assert len(calls) == 2 and len(results) == 3
    assert results[2][1].status == "unverified"
