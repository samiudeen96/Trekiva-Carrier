"""Persist an allocation run: every carrier call (request + raw response) and every verdict."""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from sqlalchemy.orm import Session

from app.carriers.types import CarrierOffer, ServiceabilityRequest
from app.logistics.allocation.types import AllocationResult, OfferFailure
from app.logistics.offers import TimedOutcome
from app.models import CarrierQuote, CarrierServiceabilityCheck, ShopifyFulfillmentOrder


def persist_run(
    db: Session,
    *,
    fo: ShopifyFulfillmentOrder,
    run_id: uuid.UUID,
    requests: Mapping[str, ServiceabilityRequest],
    outcomes: Mapping[str, TimedOutcome],
    result: AllocationResult,
) -> None:
    checks: dict[str, CarrierServiceabilityCheck] = {}
    for code, timed in outcomes.items():
        outcome = timed.outcome
        check = CarrierServiceabilityCheck(
            shop_id=fo.shop_id,
            fulfillment_order_id=fo.id,
            run_id=run_id,
            carrier_code=code,
            request=requests[code].model_dump(mode="json"),
            duration_ms=timed.duration_ms,
        )
        if isinstance(outcome, OfferFailure):
            check.error_class = outcome.error_class
            check.error_message = outcome.message
        else:
            check.serviceable = outcome.serviceable
            check.cod_available = outcome.cod_available
            check.prepaid_available = outcome.prepaid_available
            check.pickup_available = outcome.pickup_available
            check.response = outcome.model_dump(mode="json")
        db.add(check)
        checks[code] = check
    db.flush()

    strategy = result.rule.strategy.value
    for rank, cand in enumerate(result.ranked, start=1):
        db.add(
            _quote(
                fo,
                run_id,
                cand.carrier_code,
                cand.offer,
                checks,
                strategy,
                result.rule.rule_id,
                eligible=True,
                rank=rank,
                score=cand.score,
                days=cand.days,
                selected=rank == 1,
            )
        )
    for rej in result.rejected:
        db.add(
            _quote(
                fo,
                run_id,
                rej.carrier_code,
                rej.offer,
                checks,
                strategy,
                result.rule.rule_id,
                eligible=False,
                reason=rej.reason,
                detail=rej.detail,
            )
        )


def _quote(
    fo: ShopifyFulfillmentOrder,
    run_id: uuid.UUID,
    code: str,
    offer: CarrierOffer | None,
    checks: Mapping[str, CarrierServiceabilityCheck],
    strategy: str,
    rule_id: int | None,
    *,
    eligible: bool,
    rank: int | None = None,
    score: float | None = None,
    days: int | None = None,
    selected: bool = False,
    reason: str | None = None,
    detail: str | None = None,
) -> CarrierQuote:
    check = checks.get(code)
    return CarrierQuote(
        check_id=check.id if check else None,
        fulfillment_order_id=fo.id,
        run_id=run_id,
        carrier_code=code,
        cost=offer.cost if offer else None,
        currency=offer.currency if offer else None,
        transit_days=days if days is not None else (offer.transit_days if offer else None),
        edd=offer.edd if offer else None,
        score=score,
        rank=rank,
        eligible=eligible,
        rejection_reason=reason,
        rejection_detail=detail,
        selected=selected,
        strategy=strategy,
        rule_id=rule_id,
    )
