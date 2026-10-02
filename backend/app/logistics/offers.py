"""Collect offers from several carriers in parallel, isolating failures per carrier."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass

from app.carriers.base import CarrierAdapter
from app.carriers.errors import CarrierError
from app.carriers.types import ServiceabilityRequest
from app.logistics.allocation.types import OfferFailure, OfferOutcome

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TimedOutcome:
    outcome: OfferOutcome
    duration_ms: int


def _call(adapter: CarrierAdapter, request: ServiceabilityRequest) -> TimedOutcome:
    started = time.monotonic()
    try:
        outcome: OfferOutcome = adapter.get_offer(request)
    except CarrierError as exc:
        outcome = OfferFailure(adapter.code, exc.error_class, exc.message, exc.retryable)
    except Exception as exc:
        log.exception("Unexpected carrier error", extra={"carrier": adapter.code})
        outcome = OfferFailure(adapter.code, "UnexpectedError", repr(exc)[:500], True)
    return TimedOutcome(outcome, int((time.monotonic() - started) * 1000))


def collect_offers(
    adapters: Mapping[str, CarrierAdapter],
    requests: Mapping[str, ServiceabilityRequest],
    *,
    timeout_seconds: float,
) -> dict[str, TimedOutcome]:
    """Ask every carrier at once. A carrier that errors or exceeds `timeout_seconds` yields an
    `OfferFailure`; it never prevents the others from being compared."""
    if not adapters:
        return {}
    results: dict[str, TimedOutcome] = {}
    pool = ThreadPoolExecutor(max_workers=len(adapters), thread_name_prefix="offer")
    try:
        futures = {pool.submit(_call, adapters[c], requests[c]): c for c in adapters}
        done, _ = wait(futures, timeout=timeout_seconds)
        for future, code in futures.items():
            if future in done:
                results[code] = future.result()
            else:
                future.cancel()
                results[code] = TimedOutcome(
                    OfferFailure(
                        code, "OfferTimeout", f"No response within {timeout_seconds}s", True
                    ),
                    int(timeout_seconds * 1000),
                )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results
