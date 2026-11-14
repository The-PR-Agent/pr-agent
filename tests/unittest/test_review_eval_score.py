import importlib.util
import math
import pathlib

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "review_eval" / "score.py"
_spec = importlib.util.spec_from_file_location("review_eval_score", _PATH)
score = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(score)

REVIEW = """## PR Reviewer Guide

### ⚡ Recommended focus areas for review

####
**Stale Cache**

The cache is never invalidated.

**Suggested fix**

```python
cache.clear()
```

**Missing Guard**

`x` can be None here.

<details><summary><strong>⚙️ Agent run details</strong></summary></details>
"""


def metrics(**over):
    m = {"runs": 10, "failed_runs": 0, "capped_runs": 0, "known_findings": 44, "catch": 10.0, "valid": 5.0,
         "posted": 30.0, "findings_per_run": 1.0, "judge_model": "j"}
    m.update(over)
    return m


def test_findings_keeps_fix_subheading_inside_its_finding():
    found = score.findings(REVIEW)
    assert [t for t, _ in found] == ["Stale Cache", "Missing Guard"]
    assert "cache.clear()" in found[0][1]


def test_findings_empty_without_focus_section():
    assert score.findings("## PR Reviewer Guide\n\nNo key issues.") == []


@pytest.mark.parametrize("a,b,expected", [("caught", "caught", 1.0), ("caught", "missed", 0.5),
                                          ("partial", "partial", 0.5), ("Caught", "bogus", 0.5),
                                          (None, "missed", 0.0)])
def test_finding_points(a, b, expected):
    assert score.finding_points(a, b) == expected


def test_noise_floor_shrinks_with_repeats():
    assert score.noise_floor(44, 1) == pytest.approx(8.8)
    assert score.noise_floor(44, 2) == pytest.approx(8.8 / math.sqrt(2))


def test_gate_passes_small_drop_and_fails_large_drop():
    floor = score.noise_floor(44, 2)
    failures, notes = score.gate(metrics(catch=10.0 - floor + 0.1), metrics(), 2)
    assert failures == []
    assert any(n.startswith("catch") for n in notes)
    failures, _ = score.gate(metrics(catch=10.0 - floor - 0.1), metrics(), 2)
    assert len(failures) == 1 and failures[0].startswith("catch")


def test_gate_fails_on_failed_runs_and_cap_hits_without_baseline():
    failures, notes = score.gate(metrics(failed_runs=1, capped_runs=2), None, 2)
    assert len(failures) == 2
    assert notes == ["no baseline: nothing to compare with"]


def test_gate_notes_judge_model_change():
    _, notes = score.gate(metrics(), metrics(judge_model="other"), 2)
    assert any("judge model differs" in n for n in notes)
