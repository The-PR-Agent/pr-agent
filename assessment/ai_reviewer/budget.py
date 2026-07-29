from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic


class BudgetExpired(RuntimeError):
    """Raised when analysis must not start more work."""


@dataclass(frozen=True, slots=True)
class Budget:
    limit_seconds: float = 540.0
    clock: Callable[[], float] = monotonic
    _deadline: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        limit_seconds = float(self.limit_seconds)
        if not math.isfinite(limit_seconds) or limit_seconds <= 0:
            raise ValueError("limit_seconds must be finite and positive")
        object.__setattr__(self, "limit_seconds", limit_seconds)
        object.__setattr__(self, "_deadline", self.clock() + limit_seconds)

    @property
    def deadline(self) -> float:
        return self._deadline

    def remaining_seconds(self) -> float:
        return max(0.0, self._deadline - self.clock())

    def require_start(self, operation: str) -> None:
        if self.remaining_seconds() <= 0:
            raise BudgetExpired(f"budget exhausted before {operation}")
