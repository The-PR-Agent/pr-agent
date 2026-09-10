import json

from pr_agent.algo.run_details import init_run_details, record_ai_call
from pr_agent.algo.run_ledger import write_ledger


class _Usage:
    prompt_tokens = 100
    completion_tokens = 20
    total_tokens = 120
    prompt_tokens_details = type("D", (), {"cached_tokens": 60})()


def test_record_ai_call_appends_call_record():
    details = init_run_details()
    record_ai_call(_Usage(), model="m", cost_usd="0.01", stage="review", chunk_index=2,
                   sample_index=0, files=["a.py", "b.py"], latency_ms=812)

    assert len(details.calls) == 1
    call = details.calls[0]
    assert (call.stage, call.chunk_index, call.files) == ("review", 2, ("a.py", "b.py"))
    assert (call.prompt_tokens, call.cached_tokens, call.completion_tokens) == (100, 60, 20)
    assert details.total_tokens == 120


def test_write_ledger_emits_one_jsonl_row_per_call(tmp_path):
    details = init_run_details()
    record_ai_call(_Usage(), model="m", stage="review", chunk_index=0)
    record_ai_call(_Usage(), model="m", stage="verify", chunk_index=None)

    path = tmp_path / "ledger.jsonl"
    rows = write_ledger(details, str(path), run_id="r1", tool="review")

    lines = path.read_text().splitlines()
    assert rows == 2 and len(lines) == 2
    first = json.loads(lines[0])
    assert first["run_id"] == "r1" and first["stage"] == "review" and first["prompt_tokens"] == 100
    assert sum(json.loads(line)["prompt_tokens"] + json.loads(line)["completion_tokens"]
               for line in lines) == details.total_tokens
