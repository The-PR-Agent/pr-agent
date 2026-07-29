from __future__ import annotations

import pytest

from assessment.ai_reviewer.budget import Budget, BudgetExpired


class FakeClock:
    value = 100.0

    def __call__(self) -> float:
        return self.value


def test_budget_refuses_new_work_after_deadline() -> None:
    clock = FakeClock()
    budget = Budget(limit_seconds=540.0, clock=clock)

    clock.value = 640.0
    assert budget.remaining_seconds() == 0.0

    clock.value = 641.0
    with pytest.raises(BudgetExpired, match="context request"):
        budget.require_start("context request")


def test_budget_uses_the_injected_monotonic_clock() -> None:
    clock = FakeClock()
    budget = Budget(limit_seconds=10.0, clock=clock)

    clock.value = 105.0

    assert budget.remaining_seconds() == 5.0
    assert budget.deadline == 110.0


@pytest.mark.parametrize("limit", (0.0, -1.0, float("inf"), float("nan")))
def test_budget_rejects_nonpositive_or_nonfinite_limits(limit: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        Budget(limit_seconds=limit)
