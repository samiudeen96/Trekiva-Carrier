from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StepResult:
    """Outcome of one background step. `retry_in` asks the task runner to retry the same step
    after that many seconds (transient failures only)."""

    outcome: str
    detail: str = ""
    retry_in: int | None = None
