"""Carrier allocation for one fulfillment order.

    1. (transaction) re-read Shopify and re-run the gate; claim AWAITING_ALLOCATION -> ALLOCATING
    2. (no locks)    ask every eligible carrier for an offer, in parallel
    3. (transaction) rank, persist checks + quotes, move to ALLOCATED (enqueue shipment creation)
                     or NO_CARRIER_AVAILABLE with the exact reasons

Network calls never happen while a database row lock is held.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.carriers.base import CarrierAdapter
from app.carriers.errors import CarrierError
from app.carriers.types import CarrierOffer, ServiceabilityRequest
from app.core.config import get_settings
from app.core.db import session_scope
from app.core.enums import (
    INACTIVE_SHIPMENT_STATUSES,
    AllocationStrategy,
    LogisticsStatus,
    LogLevel,
)
from app.core.errors import TrekivaError
from app.logistics import quotes
from app.logistics.allocation import (
    AllocationResult,
    CarrierProfile,
    OfferFailure,
    Rejection,
    RuleConfig,
    allocate,
    prefilter,
)
from app.logistics.offers import TimedOutcome, collect_offers
from app.logistics.pipeline import sync_order
from app.logistics.plan import PlanError, ShipmentPlan, build_plan
from app.logistics.results import StepResult
from app.logistics.state import ensure_transition
from app.models import Shipment, Shop, ShopifyFulfillmentOrder
from app.services import audit, carriers, rules, shops
from app.workers import enqueue
from app.workers.retry import backoff_seconds

CARRIERS_UNREACHABLE = "CARRIERS_UNREACHABLE"


def lock_fo(db: Session, fo_id: int) -> ShopifyFulfillmentOrder:
    fo = db.scalar(
        select(ShopifyFulfillmentOrder)
        .where(ShopifyFulfillmentOrder.id == fo_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if fo is None:
        raise TrekivaError(f"Fulfillment order {fo_id} not found")
    return fo


def active_shipment(db: Session, fo_id: int) -> Shipment | None:
    return db.scalar(
        select(Shipment).where(
            Shipment.fulfillment_order_id == fo_id,
            Shipment.status.not_in(list(INACTIVE_SHIPMENT_STATUSES)),
        )
    )


def move(fo: ShopifyFulfillmentOrder, status: LogisticsStatus, reason: str, detail: str) -> None:
    ensure_transition(fo.logistics_status, status)
    fo.logistics_status = status
    fo.status_reason = reason
    fo.status_detail = detail


def log(
    db: Session,
    fo: ShopifyFulfillmentOrder,
    step: audit.Step,
    message: str,
    *,
    level: LogLevel = LogLevel.INFO,
    run_id: uuid.UUID | None = None,
    data: dict[str, Any] | None = None,
    actor: str = "system",
    shipment_id: int | None = None,
) -> None:
    audit.record(
        db,
        shop_id=fo.shop_id,
        order_id=fo.order_id,
        fulfillment_order_id=fo.id,
        shipment_id=shipment_id,
        run_id=run_id,
        step=step,
        level=level,
        message=f"{fo.order.name}: {message}",
        data=data,
        actor=actor,
    )


@dataclass
class _Prepared:
    run_id: uuid.UUID
    plan: ShipmentPlan
    profiles: dict[str, CarrierProfile]
    rule_configs: list[RuleConfig]
    pre_rejected: list[Rejection]
    adapters: dict[str, CarrierAdapter]
    requests: dict[str, ServiceabilityRequest]


def _prepare(db: Session, shop: Shop, fo: ShopifyFulfillmentOrder, plan: ShipmentPlan) -> _Prepared:
    profiles = carriers.load_profiles(db, shop)
    rule_configs = rules.load_rule_configs(db, shop)
    if fo.forced_carrier_code:
        # Staff choice: only that carrier, rule limits ignored, hard checks still apply.
        profiles = {k: v for k, v in profiles.items() if k == fo.forced_carrier_code}
        rule_configs = [
            RuleConfig(
                strategy=AllocationStrategy.FASTEST, name=f"Manual selection by {fo.selected_by}"
            )
        ]
    eligible, pre_rejected = prefilter(plan.context, profiles)
    adapters: dict[str, CarrierAdapter] = {}
    requests: dict[str, ServiceabilityRequest] = {}
    for code in eligible:
        try:
            adapter = carriers.build_adapter(db, shop, code)
        except (TrekivaError, CarrierError) as exc:
            pre_rejected.append(Rejection(code, "ADAPTER_UNAVAILABLE", str(exc)))
            continue
        adapters[code] = adapter
        ref = carriers.warehouse_ref(db, adapter.config.account_id, plan.warehouse_id)
        requests[code] = plan.serviceability_request(ref)
    return _Prepared(uuid.uuid4(), plan, profiles, rule_configs, pre_rejected, adapters, requests)


def _decide(prep: _Prepared, outcomes: dict[str, TimedOutcome]) -> AllocationResult:
    return allocate(
        prep.plan.context,
        prep.profiles,
        {code: timed.outcome for code, timed in outcomes.items()},
        prep.rule_configs,
        prefilter_rejections=prep.pre_rejected,
    )


def run_allocation(
    fo_id: int,
    *,
    trigger: str,
    admin_factory: shops.AdminFactory = shops.default_admin_factory,
    is_retry: bool = False,
) -> StepResult:
    settings = get_settings()

    # --- 1. live re-check + claim ------------------------------------------------------------
    with session_scope() as db:
        fo = db.get(ShopifyFulfillmentOrder, fo_id)
        if fo is None:
            return StepResult("missing")
        shop = db.get(Shop, fo.shop_id)
        assert shop is not None
        if not fo.staff_initiated and not shop.automation_enabled:
            return StepResult("automation_off")
        if (
            is_retry
            and fo.logistics_status == LogisticsStatus.NO_CARRIER_AVAILABLE
            and fo.status_reason == CARRIERS_UNREACHABLE
        ):
            move(
                fo,
                LogisticsStatus.AWAITING_ALLOCATION,
                "RETRYING",
                "Retrying carriers that were unreachable",
            )
            db.flush()
        if fo.logistics_status != LogisticsStatus.AWAITING_ALLOCATION:
            return StepResult("not_ready", detail=fo.logistics_status.value)

        sync_order(
            db,
            shop,
            admin_factory(db, shop),
            fo.order.shopify_order_id,
            trigger=f"allocation:{trigger}",
        )
        fo = lock_fo(db, fo_id)
        if fo.logistics_status != LogisticsStatus.AWAITING_ALLOCATION:
            return StepResult("blocked", detail=fo.status_detail or fo.logistics_status.value)
        existing = active_shipment(db, fo.id)
        if existing is not None:
            log(
                db,
                fo,
                audit.Step.ERROR,
                f"allocation skipped: active shipment {existing.awb or existing.id} already exists",
                level=LogLevel.ERROR,
                shipment_id=existing.id,
            )
            return StepResult("has_shipment")
        try:
            plan = build_plan(db, fo.order, fo)
        except PlanError as exc:
            move(fo, LogisticsStatus.FAILED, exc.reason, exc.detail)
            log(db, fo, audit.Step.ERROR, exc.detail, level=LogLevel.ERROR)
            return StepResult("failed", detail=exc.detail)

        prep = _prepare(db, shop, fo, plan)
        if not fo.staff_initiated:
            fo.selected_by = "system"
        move(fo, LogisticsStatus.ALLOCATING, "CHECKING_CARRIERS", "Checking carrier serviceability")
        fo.allocation_run_id = prep.run_id
        log(
            db,
            fo,
            audit.Step.ALLOCATION_STARTED,
            f"checking serviceability with {', '.join(prep.adapters) or 'no eligible carrier'}",
            run_id=prep.run_id,
            data={
                "trigger": trigger,
                "pincode": plan.delivery.pincode,
                "payment_mode": plan.payment_mode.value,
                "weight_g": plan.parcel.weight_g,
            },
        )

    # --- 2. carrier offers (no locks held) -----------------------------------------------------
    outcomes = collect_offers(
        prep.adapters, prep.requests, timeout_seconds=settings.carrier_offer_timeout_seconds
    )
    result = _decide(prep, outcomes)

    # --- 3. decide + persist -------------------------------------------------------------------
    with session_scope() as db:
        fo = lock_fo(db, fo_id)
        if fo.logistics_status != LogisticsStatus.ALLOCATING or fo.allocation_run_id != prep.run_id:
            return StepResult("superseded")
        quotes.persist_run(
            db, fo=fo, run_id=prep.run_id, requests=prep.requests, outcomes=outcomes, result=result
        )
        for code, timed in sorted(outcomes.items()):
            log(
                db,
                fo,
                audit.Step.SERVICEABILITY_CHECKED,
                _describe(code, timed),
                run_id=prep.run_id,
                level=LogLevel.WARNING
                if isinstance(timed.outcome, OfferFailure)
                else LogLevel.INFO,
            )

        selected = result.selected
        if selected is None:
            transient = any(
                isinstance(t.outcome, OfferFailure) and t.outcome.retryable
                for t in outcomes.values()
            )
            reason = CARRIERS_UNREACHABLE if transient else "NO_CARRIER"
            move(fo, LogisticsStatus.NO_CARRIER_AVAILABLE, reason, result.explain())
            log(
                db,
                fo,
                audit.Step.NO_CARRIER,
                result.explain(),
                level=LogLevel.WARNING,
                run_id=prep.run_id,
            )
            return StepResult(
                "no_carrier",
                detail=result.explain(),
                retry_in=backoff_seconds(0) if transient else None,
            )

        fo.selected_carrier_code = selected.carrier_code
        fo.allocation_ranking = [
            _ranking_entry(c.carrier_code, c.offer, c.days, c.score) for c in result.ranked
        ]
        move(fo, LogisticsStatus.ALLOCATED, "CARRIER_SELECTED", result.explain())
        log(
            db,
            fo,
            audit.Step.CARRIER_SELECTED,
            result.explain(),
            run_id=prep.run_id,
            data={"ranking": fo.allocation_ranking, "rule": result.rule.name},
        )
        enqueue.enqueue_after_commit(db, enqueue.CREATE_SHIPMENT, fo.id)
        return StepResult("allocated", detail=selected.carrier_code)


def preview_allocation(db: Session, shop: Shop, fo: ShopifyFulfillmentOrder) -> dict[str, Any]:
    """Re-run serviceability and allocation for staff without changing any state.

    Calls carriers (with the DB transaction open: this is an explicit, rare staff action) and
    stores the checks and quotes so the raw carrier responses can be inspected."""
    plan = build_plan(db, fo.order, fo)
    prep = _prepare(db, shop, fo, plan)
    outcomes = collect_offers(
        prep.adapters, prep.requests, timeout_seconds=get_settings().carrier_offer_timeout_seconds
    )
    result = _decide(prep, outcomes)
    quotes.persist_run(
        db, fo=fo, run_id=prep.run_id, requests=prep.requests, outcomes=outcomes, result=result
    )
    return {
        "run_id": str(prep.run_id),
        "rule": result.rule.name,
        "strategy": result.rule.strategy.value,
        "selected": result.selected.carrier_code if result.selected else None,
        "explanation": result.explain(),
        "ranked": [_ranking_entry(c.carrier_code, c.offer, c.days, c.score) for c in result.ranked],
        "rejected": [
            {"carrier_code": r.carrier_code, "reason": r.reason, "detail": r.detail}
            for r in result.rejected
        ],
    }


def _ranking_entry(
    code: str, offer: CarrierOffer, days: int | None, score: float | None
) -> dict[str, Any]:
    return {
        "carrier_code": code,
        "cost": str(offer.cost) if offer.cost is not None else None,
        "currency": offer.currency,
        "edd": offer.edd.isoformat() if offer.edd else None,
        "days": days,
        "score": score,
        "failed": False,
    }


def _describe(code: str, timed: TimedOutcome) -> str:
    o = timed.outcome
    if isinstance(o, OfferFailure):
        return f"{code} serviceability check failed ({o.error_class}): {o.message}"
    if not o.serviceable:
        return f"{code} not serviceable{': ' + o.reason if o.reason else ''}"
    days = (
        f"{o.transit_days} days"
        if o.transit_days is not None
        else (f"EDD {o.edd}" if o.edd else "EDD unknown")
    )
    cost = (
        f"₹{o.cost}"
        if o.cost is not None and o.currency == "INR"
        else (f"{o.cost} {o.currency}" if o.cost is not None else "no rate")
    )
    cod = "" if o.cod_available is None else (", COD ok" if o.cod_available else ", no COD")
    return f"{code} serviceable: {days}, {cost}{cod}"


def ranking_entry_for(fo: ShopifyFulfillmentOrder, code: str) -> dict[str, Any] | None:
    return next((e for e in fo.allocation_ranking if e.get("carrier_code") == code), None)


def entry_cost(entry: dict[str, Any] | None) -> Decimal | None:
    return Decimal(entry["cost"]) if entry and entry.get("cost") is not None else None
