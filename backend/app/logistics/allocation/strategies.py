"""Ranking strategies. Each takes already-eligible candidates and returns them best-first.

Ties are always broken deterministically (priority, then carrier code) so the same inputs give
the same carrier every time.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from decimal import Decimal

from app.core.enums import AllocationStrategy
from app.logistics.allocation.types import Candidate, StrategyParams

_INF = Decimal("Infinity")

Strategy = Callable[[Sequence[Candidate], StrategyParams], list[Candidate]]


def _days(c: Candidate) -> float:
    return float("inf") if c.days is None else float(c.days)


def _cost(c: Candidate) -> Decimal:
    return _INF if c.offer.cost is None else c.offer.cost


def _priority(c: Candidate, params: StrategyParams) -> int:
    if params.carrier_order and c.carrier_code in params.carrier_order:
        return params.carrier_order.index(c.carrier_code) - len(params.carrier_order)
    return c.profile.priority


def fastest(cands: Sequence[Candidate], params: StrategyParams) -> list[Candidate]:
    return sorted(cands, key=lambda c: (_days(c), _cost(c), _priority(c, params), c.carrier_code))


def cheapest(cands: Sequence[Candidate], params: StrategyParams) -> list[Candidate]:
    return sorted(cands, key=lambda c: (_cost(c), _days(c), _priority(c, params), c.carrier_code))


def priority(cands: Sequence[Candidate], params: StrategyParams) -> list[Candidate]:
    return sorted(cands, key=lambda c: (_priority(c, params), _days(c), _cost(c), c.carrier_code))


def custom(cands: Sequence[Candidate], params: StrategyParams) -> list[Candidate]:
    """Explicit preference order (`carrier_order`); unlisted carriers follow, fastest first."""
    order = params.carrier_order

    def key(c: Candidate) -> tuple[int, float, Decimal, str]:
        idx = order.index(c.carrier_code) if c.carrier_code in order else len(order)
        return (idx, _days(c), _cost(c), c.carrier_code)

    return sorted(cands, key=key)


def _normalise(values: list[float | None]) -> list[float]:
    present = [v for v in values if v is not None]
    if not present:
        return [1.0 for _ in values]
    lo, hi = min(present), max(present)
    span = hi - lo
    return [1.0 if v is None else (0.0 if span == 0 else (v - lo) / span) for v in values]


def balanced(cands: Sequence[Candidate], params: StrategyParams) -> list[Candidate]:
    """Weighted score (lower is better) over normalised speed, cost, performance and priority."""
    if not cands:
        return []
    w = params.weights
    speed = _normalise([None if c.days is None else float(c.days) for c in cands])
    cost = _normalise([None if c.offer.cost is None else float(c.offer.cost) for c in cands])
    prio = _normalise([float(_priority(c, params)) for c in cands])
    scored = []
    for i, c in enumerate(cands):
        perf_value = c.offer.performance_score or c.profile.performance_score
        perf = 0.5 if perf_value is None else 1.0 - min(float(perf_value), 100.0) / 100.0
        total = w.speed * speed[i] + w.cost * cost[i] + w.performance * perf + w.priority * prio[i]
        scored.append(replace(c, score=round(total, 6)))
    return sorted(scored, key=lambda c: (c.score, _days(c), _cost(c), c.carrier_code))


STRATEGIES: dict[AllocationStrategy, Strategy] = {
    AllocationStrategy.FASTEST: fastest,
    AllocationStrategy.CHEAPEST: cheapest,
    AllocationStrategy.PRIORITY: priority,
    AllocationStrategy.BALANCED: balanced,
    AllocationStrategy.CUSTOM: custom,
}
