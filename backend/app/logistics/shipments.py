"""Shipment creation, reconciliation, staff resolution and cancellation.

Creation is "intent before call":

    A. (transaction, FO row lock) re-read Shopify and re-run the gate. If still ALLOCATED, insert
       the shipment as CREATING with idempotency_key `{fo_id}:{attempt}` and COMMIT. The partial
       unique index guarantees no second active shipment can exist for this FO.
    B. (no locks) call the carrier with that idempotency key as our reference.
    C. (transaction) record the outcome:
         success              -> AWB_CREATED, FO SHIPPED, enqueue Shopify sync
         CarrierAmbiguousError -> CREATION_UNKNOWN, FO RECONCILING (same carrier is asked
                                 whether it created it; we NEVER try another carrier meanwhile)
         transient            -> CREATE_FAILED, retry the same carrier with backoff, then fall back
         validation/auth/...  -> CREATE_FAILED, fall back to the next ranked carrier, else FAILED

A crash between A and C leaves a CREATING shipment that the sweeper turns into CREATION_UNKNOWN,
which is reconciled exactly like a timeout.
"""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import alerts
from app.carriers.base import CarrierAdapter
from app.carriers.errors import (
    CarrierAmbiguousError,
    CarrierError,
    CarrierNotSupportedError,
    CarrierTransientError,
)
from app.carriers.types import CreateShipmentRequest, ShipmentRef, ShipmentResult
from app.core.db import session_scope
from app.core.enums import (
    AlertSeverity,
    LogisticsStatus,
    LogLevel,
    ShipmentStatus,
    ShopifySyncStatus,
    TrackingStatus,
)
from app.core.errors import ConflictError, NotFoundError, TrekivaError, ValidationFailed
from app.core.time import utcnow
from app.logistics.allocation_run import (
    active_shipment,
    entry_cost,
    lock_fo,
    log,
    move,
    ranking_entry_for,
)
from app.logistics.pipeline import sync_order
from app.logistics.plan import PlanError, build_plan
from app.logistics.results import StepResult
from app.models import Shipment, Shop, ShopifyFulfillmentOrder
from app.schemas.settings import ShopSettings
from app.services import audit, carriers, shops
from app.tracking import service as tracking
from app.tracking.transitions import CANCELLABLE
from app.workers import enqueue
from app.workers.retry import backoff_seconds

logger = logging.getLogger(__name__)

MAX_RECONCILE_ATTEMPTS = 6
CANCEL_ALERT_AFTER_RETRIES = 3
"""A retryable cancellation still failing after this many retries is alerted (once)."""
_CLAIMED_ELSEWHERE = frozenset(
    {LogisticsStatus.SHIPMENT_PENDING, LogisticsStatus.RECONCILING, LogisticsStatus.SHIPPED}
)
STAFF_RECONCILE = "RECONCILIATION_NEEDS_STAFF"


def lock_shipment(db: Session, shipment_id: int) -> Shipment:
    shipment = db.scalar(
        select(Shipment)
        .where(Shipment.id == shipment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if shipment is None:
        raise NotFoundError(f"Shipment {shipment_id} not found")
    return shipment


def _attempts(db: Session, fo_id: int, carrier_code: str | None = None) -> int:
    stmt = select(func.count()).select_from(Shipment).where(Shipment.fulfillment_order_id == fo_id)
    if carrier_code is not None:
        stmt = stmt.where(
            Shipment.carrier_code == carrier_code, Shipment.status == ShipmentStatus.CREATE_FAILED
        )
    return int(db.scalar(stmt) or 0)


def _ref(shipment: Shipment) -> ShipmentRef:
    return ShipmentRef(
        idempotency_key=shipment.idempotency_key,
        carrier_shipment_id=shipment.carrier_shipment_id,
        awb=shipment.awb,
    )


# --- creation ----------------------------------------------------------------------------------


def create_shipment(
    fo_id: int, *, admin_factory: shops.AdminFactory = shops.default_admin_factory
) -> StepResult:
    # --- A. reserve -------------------------------------------------------------------------
    with session_scope() as db:
        fo = db.get(ShopifyFulfillmentOrder, fo_id)
        if fo is None or fo.logistics_status != LogisticsStatus.ALLOCATED:
            return StepResult("not_allocated")
        shop = db.get(Shop, fo.shop_id)
        assert shop is not None
        if not fo.staff_initiated and not shop.automation_enabled:
            return StepResult("automation_off")

        # Live hold check, immediately before the courier call (holds win, always).
        sync_order(
            db,
            shop,
            admin_factory(db, shop),
            fo.order.shopify_order_id,
            trigger="pre-shipment-check",
        )
        fo = lock_fo(db, fo_id)
        if fo.logistics_status in _CLAIMED_ELSEWHERE:
            return StepResult("not_allocated")  # another worker got here first
        if fo.logistics_status != LogisticsStatus.ALLOCATED:
            log(
                db,
                fo,
                audit.Step.SHIPMENT_BLOCKED,
                f"shipment not created: {fo.status_detail or fo.logistics_status.value}",
                level=LogLevel.WARNING,
            )
            return StepResult("blocked", detail=fo.status_detail or "")
        if (existing := active_shipment(db, fo.id)) is not None:
            log(
                db,
                fo,
                audit.Step.ERROR,
                f"duplicate shipment prevented: {existing.carrier_code} "
                f"{existing.awb or existing.status.value} already active",
                level=LogLevel.ERROR,
                shipment_id=existing.id,
            )
            return StepResult("duplicate_prevented")

        code = fo.selected_carrier_code
        try:
            if not code:
                raise PlanError("NO_SELECTED_CARRIER", "No carrier selected")
            plan = build_plan(db, fo.order, fo)
            adapter = carriers.build_adapter(db, shop, code)
        except (PlanError, TrekivaError, CarrierError) as exc:
            detail = exc.detail if isinstance(exc, PlanError) else str(exc)
            move(
                fo, LogisticsStatus.FAILED, getattr(exc, "reason", "SHIPMENT_SETUP_FAILED"), detail
            )
            log(db, fo, audit.Step.SHIPMENT_FAILED, detail, level=LogLevel.ERROR)
            if isinstance(exc, CarrierError) and code:
                alerts.carrier_error(
                    db,
                    shop_id=fo.shop_id,
                    carrier_code=code,
                    error_class=exc.error_class,
                    message=exc.message,
                    during="shipment setup",
                    order_id=fo.order_id,
                    fulfillment_order_id=fo.id,
                )
            return StepResult("failed", detail=detail)

        attempt = _attempts(db, fo.id) + 1
        key = f"{fo.id}:{attempt}"
        entry = ranking_entry_for(fo, code)
        shipment = Shipment(
            shop_id=shop.id,
            order_id=fo.order_id,
            fulfillment_order_id=fo.id,
            shopify_order_id=fo.order.shopify_order_id,
            shopify_order_name=fo.order.name,
            shopify_fulfillment_order_id=fo.shopify_fulfillment_order_id,
            carrier_code=code,
            carrier_account_id=adapter.config.account_id,
            warehouse_id=plan.warehouse_id,
            idempotency_key=key,
            attempt=attempt,
            status=ShipmentStatus.CREATING,
            shipping_cost=entry_cost(entry),
            currency=(entry or {}).get("currency") or plan.currency,
            estimated_delivery_date=_edd(entry),
            cod_amount=plan.cod_amount,
            declared_value=plan.declared_value,
            weight_g=plan.parcel.weight_g,
            dimensions=plan.parcel.model_dump(mode="json", exclude={"weight_g"}),
            created_by=fo.selected_by or "system",
        )
        try:
            with db.begin_nested():
                db.add(shipment)
                db.flush()
        except IntegrityError:
            log(
                db,
                fo,
                audit.Step.ERROR,
                "duplicate shipment prevented by database constraint",
                level=LogLevel.ERROR,
            )
            return StepResult("duplicate_prevented")
        move(
            fo,
            LogisticsStatus.SHIPMENT_PENDING,
            "CREATING_SHIPMENT",
            f"Creating shipment with {code}",
        )
        ref = carriers.warehouse_ref(db, adapter.config.account_id, plan.warehouse_id)
        request = plan.create_request(
            idempotency_key=key, order=fo.order, fo=fo, carrier_warehouse_ref=ref
        )
        log(
            db,
            fo,
            audit.Step.SHIPMENT_REQUESTED,
            f"creating shipment with {code} (attempt {attempt}, ref {key})",
            shipment_id=shipment.id,
            data={"request": request.model_dump(mode="json")},
        )
        shipment_id = shipment.id

    # --- B. carrier call --------------------------------------------------------------------
    result: ShipmentResult | None = None
    failure: CarrierError | Exception | None = None
    try:
        result = _create_with_awb(adapter, request)
    except Exception as exc:  # classified below; unknown errors are treated as ambiguous
        failure = exc

    # --- C. record outcome ------------------------------------------------------------------
    with session_scope() as db:
        fo = lock_fo(db, fo_id)
        shipment = lock_shipment(db, shipment_id)
        shop = db.get(Shop, fo.shop_id)
        assert shop is not None
        if result is not None:
            mark_created(db, shop, fo, shipment, result, via="carrier response")
            return StepResult("created", detail=shipment.awb or "")
        assert failure is not None
        message = f"{type(failure).__name__}: {failure}"
        shipment.last_error = message[:2000]
        raw = getattr(failure, "raw", None)
        if raw is not None:
            shipment.raw_create_response = {"error": raw}

        if isinstance(failure, CarrierAmbiguousError) or not isinstance(failure, CarrierError):
            shipment.status = ShipmentStatus.CREATION_UNKNOWN
            move(
                fo,
                LogisticsStatus.RECONCILING,
                "CREATION_UNKNOWN",
                f"{code} did not confirm the shipment ({message}). Checking with {code} before any retry.",
            )
            log(
                db,
                fo,
                audit.Step.SHIPMENT_UNCERTAIN,
                fo.status_detail or "",
                level=LogLevel.WARNING,
                shipment_id=shipment.id,
            )
            enqueue.enqueue_after_commit(db, enqueue.RECONCILE, shipment.id, countdown=30)
            return StepResult("ambiguous", detail=message)

        shipment.status = ShipmentStatus.CREATE_FAILED
        settings = ShopSettings.load(shop.settings)
        if isinstance(failure, CarrierTransientError):
            tries = _attempts(db, fo.id, code)
            if tries < settings.max_create_attempts_per_carrier:
                move(
                    fo,
                    LogisticsStatus.ALLOCATED,
                    "CARRIER_RETRY",
                    f"{code} temporarily failed ({message}); retrying",
                )
                log(
                    db,
                    fo,
                    audit.Step.SHIPMENT_FAILED,
                    f"{code} temporarily failed, retry {tries}/"
                    f"{settings.max_create_attempts_per_carrier - 1}: {message}",
                    level=LogLevel.WARNING,
                    shipment_id=shipment.id,
                )
                return StepResult("retry", detail=message, retry_in=backoff_seconds(tries - 1))
        log(
            db,
            fo,
            audit.Step.SHIPMENT_FAILED,
            f"{code} rejected the shipment: {message}",
            level=LogLevel.ERROR,
            shipment_id=shipment.id,
        )
        alerts.carrier_error(
            db,
            shop_id=fo.shop_id,
            carrier_code=code,
            error_class=type(failure).__name__,
            message=str(failure),
            during="shipment creation",
            order_id=fo.order_id,
            fulfillment_order_id=fo.id,
            shipment_id=shipment.id,
        )
        return _fallback_or_fail(db, fo, code, message)


def _create_with_awb(adapter: CarrierAdapter, request: CreateShipmentRequest) -> ShipmentResult:
    result = adapter.create_shipment(request)
    if not result.awb:
        awb = adapter.generate_awb(
            ShipmentRef(
                idempotency_key=request.idempotency_key,
                carrier_shipment_id=result.carrier_shipment_id,
            )
        )
        result = result.model_copy(
            update={
                "awb": awb.awb,
                "tracking_url": awb.tracking_url or result.tracking_url,
                "label_url": awb.label_url or result.label_url,
            }
        )
    return result


def _edd(entry: dict[str, object] | None) -> date | None:
    value = (entry or {}).get("edd")
    return date.fromisoformat(str(value)) if value else None


def _fallback_or_fail(
    db: Session, fo: ShopifyFulfillmentOrder, failed_code: str, message: str
) -> StepResult:
    fo.allocation_ranking = [
        {**e, "failed": True} if e.get("carrier_code") == failed_code else e
        for e in fo.allocation_ranking
    ]
    nxt = None
    if not fo.forced_carrier_code:
        nxt = next((e["carrier_code"] for e in fo.allocation_ranking if not e.get("failed")), None)
    if nxt is not None:
        fo.selected_carrier_code = nxt
        move(
            fo,
            LogisticsStatus.ALLOCATED,
            "CARRIER_FALLBACK",
            f"{failed_code} failed ({message}); trying {nxt}",
        )
        log(
            db,
            fo,
            audit.Step.CARRIER_FALLBACK,
            f"falling back from {failed_code} to {nxt}",
            level=LogLevel.WARNING,
        )
        enqueue.enqueue_after_commit(db, enqueue.CREATE_SHIPMENT, fo.id)
        return StepResult("fallback", detail=nxt)
    move(
        fo,
        LogisticsStatus.FAILED,
        "SHIPMENT_CREATION_FAILED",
        f"Shipment could not be created: {failed_code}: {message}. Fix the cause, then retry or pick a carrier.",
    )
    log(db, fo, audit.Step.SHIPMENT_FAILED, fo.status_detail or "", level=LogLevel.ERROR)
    return StepResult("failed", detail=message)


def mark_created(
    db: Session,
    shop: Shop,
    fo: ShopifyFulfillmentOrder,
    shipment: Shipment,
    result: ShipmentResult,
    *,
    via: str,
    actor: str = "system",
) -> None:
    shipment.status = ShipmentStatus.AWB_CREATED
    shipment.carrier_shipment_id = result.carrier_shipment_id or shipment.carrier_shipment_id
    shipment.awb = result.awb
    shipment.tracking_url = result.tracking_url
    shipment.label_url = result.label_url
    if result.cost is not None:
        shipment.shipping_cost = result.cost
        shipment.currency = result.currency or shipment.currency
    if result.edd is not None:
        shipment.estimated_delivery_date = result.edd
    shipment.raw_create_response = result.raw
    shipment.last_error = None
    shipment.shopify_sync_status = ShopifySyncStatus.PENDING
    tracking.schedule_next_poll(shipment)
    tracking.record_system_event(db, shipment, TrackingStatus.AWB_CREATED, "Shipment manifested")
    move(
        fo,
        LogisticsStatus.SHIPPED,
        "SHIPMENT_CREATED",
        f"{shipment.carrier_code} AWB {shipment.awb}",
    )
    fo.forced_carrier_code = None
    fo.selected_by = None
    log(
        db,
        fo,
        audit.Step.SHIPMENT_CREATED,
        f"shipment created with {shipment.carrier_code} (id {shipment.carrier_shipment_id}, via {via})",
        shipment_id=shipment.id,
        actor=actor,
        data={"response": result.raw},
    )
    log(
        db,
        fo,
        audit.Step.AWB_GENERATED,
        f"AWB {shipment.awb} generated",
        shipment_id=shipment.id,
        actor=actor,
    )
    enqueue.enqueue_after_commit(db, enqueue.SYNC_SHOPIFY, shipment.id)


# --- reconciliation ----------------------------------------------------------------------------


def reconcile_shipment(shipment_id: int) -> StepResult:
    """Ask the SAME carrier whether a shipment whose creation timed out actually exists."""
    with session_scope() as db:
        shipment = lock_shipment(db, shipment_id)
        if shipment.status != ShipmentStatus.CREATION_UNKNOWN:
            return StepResult("not_needed")
        shop = db.get(Shop, shipment.shop_id)
        assert shop is not None
        shipment.reconcile_attempts += 1
        attempt = shipment.reconcile_attempts
        adapter = carriers.adapter_for_shipment(db, shipment)
        key = shipment.idempotency_key

    found: ShipmentResult | None = None
    error: CarrierError | None = None
    try:
        found = adapter.find_shipment_by_reference(key)
    except CarrierError as exc:
        error = exc

    with session_scope() as db:
        shipment = lock_shipment(db, shipment_id)
        fo = lock_fo(db, shipment.fulfillment_order_id)
        shop = db.get(Shop, shipment.shop_id)
        assert shop is not None
        if shipment.status != ShipmentStatus.CREATION_UNKNOWN:
            return StepResult("not_needed")
        if error is not None:
            alerts.carrier_error(
                db,
                shop_id=shipment.shop_id,
                carrier_code=shipment.carrier_code,
                error_class=error.error_class,
                message=error.message,
                during="reconciliation",
                order_id=shipment.order_id,
                fulfillment_order_id=fo.id,
                shipment_id=shipment.id,
            )
            if (
                isinstance(error, CarrierNotSupportedError)
                or attempt >= MAX_RECONCILE_ATTEMPTS
                or not error.retryable
            ):
                fo.status_reason = STAFF_RECONCILE
                fo.status_detail = (
                    f"{shipment.carrier_code} could not confirm whether reference {key} was created "
                    f"({error.message}). Check the carrier panel, then mark it created (with AWB) or not created."
                )
                log(
                    db,
                    fo,
                    audit.Step.SHIPMENT_UNCERTAIN,
                    fo.status_detail,
                    level=LogLevel.ERROR,
                    shipment_id=shipment.id,
                )
                return StepResult("needs_staff", detail=error.message)
            return StepResult("retry", detail=error.message, retry_in=backoff_seconds(attempt))
        if found is not None and found.awb:
            mark_created(db, shop, fo, shipment, found, via="reconciliation")
            return StepResult("created", detail=found.awb)
        return _confirmed_not_created(
            db, shop, fo, shipment, "carrier confirmed it was not created"
        )


def _confirmed_not_created(
    db: Session,
    shop: Shop,
    fo: ShopifyFulfillmentOrder,
    shipment: Shipment,
    why: str,
    actor: str = "system",
) -> StepResult:
    shipment.status = ShipmentStatus.CREATE_FAILED
    shipment.last_error = f"Not created: {why}"
    log(
        db,
        fo,
        audit.Step.SHIPMENT_FAILED,
        f"{shipment.carrier_code} reference {shipment.idempotency_key}: {why}",
        level=LogLevel.WARNING,
        shipment_id=shipment.id,
        actor=actor,
    )
    settings = ShopSettings.load(shop.settings)
    if _attempts(db, fo.id, shipment.carrier_code) < settings.max_create_attempts_per_carrier:
        move(fo, LogisticsStatus.ALLOCATED, "CARRIER_RETRY", f"Retrying {shipment.carrier_code}")
        enqueue.enqueue_after_commit(db, enqueue.CREATE_SHIPMENT, fo.id)
        return StepResult("retry_same_carrier")
    return _fallback_or_fail(db, fo, shipment.carrier_code, why)


def resolve_unknown(
    db: Session,
    shop: Shop,
    shipment_id: int,
    *,
    created: bool,
    awb: str | None,
    carrier_shipment_id: str | None,
    actor: str,
) -> Shipment:
    """Staff resolution of a CREATION_UNKNOWN shipment after checking the carrier's panel."""
    shipment = lock_shipment(db, shipment_id)
    if shipment.shop_id != shop.id:
        raise NotFoundError("Shipment not found")
    if shipment.status != ShipmentStatus.CREATION_UNKNOWN:
        raise ConflictError("Only shipments with an unknown creation outcome can be resolved")
    fo = lock_fo(db, shipment.fulfillment_order_id)
    if created:
        if not awb:
            raise ValidationFailed("Enter the AWB shown in the carrier panel")
        url = carriers.adapter_for_shipment(db, shipment).tracking_url(awb)
        mark_created(
            db,
            shop,
            fo,
            shipment,
            ShipmentResult(
                carrier_shipment_id=carrier_shipment_id,
                awb=awb,
                tracking_url=url,
                raw={"resolved_by": actor},
            ),
            via=f"staff resolution by {actor}",
            actor=actor,
        )
    else:
        _confirmed_not_created(
            db, shop, fo, shipment, f"staff confirmed not created ({actor})", actor=actor
        )
    return shipment


# --- cancellation ------------------------------------------------------------------------------


def cancel_shipment(
    shipment_id: int,
    *,
    reallocate: bool,
    actor: str,
    reason: str,
    admin_factory: shops.AdminFactory = shops.default_admin_factory,
    attempt: int = 0,
) -> StepResult:
    """Cancel with the carrier first; only then free the fulfillment order for another carrier."""
    with session_scope() as db:
        shipment = lock_shipment(db, shipment_id)
        if shipment.status == ShipmentStatus.CANCELLED:
            return StepResult("already_cancelled")
        if (
            shipment.status not in CANCELLABLE
            and shipment.status != ShipmentStatus.CANCEL_REQUESTED
        ):
            return StepResult(
                "not_cancellable",
                detail=f"Cannot cancel a shipment in status {shipment.status.value}",
            )
        if shipment.status == ShipmentStatus.CREATION_UNKNOWN:
            return StepResult(
                "not_cancellable", detail="Resolve the unknown creation outcome first"
            )
        shop = db.get(Shop, shipment.shop_id)
        assert shop is not None
        previous = shipment.status
        shipment.status = ShipmentStatus.CANCEL_REQUESTED
        adapter = carriers.adapter_for_shipment(db, shipment)
        ref = _ref(shipment)

    error: CarrierError | None = None
    cancelled = False
    try:
        res = adapter.cancel_shipment(ref)
        cancelled = res.cancelled
        refusal = res.reason
    except CarrierError as exc:
        error = exc
        refusal = exc.message

    with session_scope() as db:
        shipment = lock_shipment(db, shipment_id)
        fo = lock_fo(db, shipment.fulfillment_order_id)
        shop = db.get(Shop, shipment.shop_id)
        assert shop is not None
        if not cancelled:
            shipment.status = (
                previous
                if previous != ShipmentStatus.CANCEL_REQUESTED
                else ShipmentStatus.AWB_CREATED
            )
            shipment.last_error = f"Cancellation failed: {refusal}"
            log(
                db,
                fo,
                audit.Step.SHIPMENT_CANCEL_FAILED,
                f"{shipment.carrier_code} did not cancel AWB {shipment.awb}: {refusal}",
                level=LogLevel.ERROR,
                shipment_id=shipment.id,
                actor=actor,
            )
            retry = backoff_seconds(0) if error is not None and error.retryable else None
            if error is not None:
                alerts.carrier_error(
                    db,
                    shop_id=shipment.shop_id,
                    carrier_code=shipment.carrier_code,
                    error_class=error.error_class,
                    message=error.message,
                    during="cancellation",
                    order_id=shipment.order_id,
                    fulfillment_order_id=fo.id,
                    shipment_id=shipment.id,
                )
            if retry is None or attempt == CANCEL_ALERT_AFTER_RETRIES:
                alerts.raise_alert(
                    db,
                    kind=alerts.AlertKind.CARRIER_CANCEL_FAILED,
                    severity=AlertSeverity.ERROR,
                    title="Carrier did not cancel a shipment",
                    detail=f"{shipment.carrier_code} AWB {shipment.awb}: {refusal}. "
                    "Cancel it in the carrier panel before it is picked up.",
                    shop_id=shipment.shop_id,
                    order_id=shipment.order_id,
                    fulfillment_order_id=fo.id,
                    shipment_id=shipment.id,
                )
            return StepResult("cancel_failed", detail=refusal or "", retry_in=retry)

        shipment.status = ShipmentStatus.CANCELLED
        shipment.cancelled_at = utcnow()
        shipment.cancel_reason = reason
        shipment.next_poll_at = None
        log(
            db,
            fo,
            audit.Step.SHIPMENT_CANCELLED,
            f"{shipment.carrier_code} AWB {shipment.awb} cancelled: {reason}",
            shipment_id=shipment.id,
            actor=actor,
        )
        if shipment.shopify_fulfillment_id:
            try:
                admin_factory(db, shop).cancel_fulfillment(shipment.shopify_fulfillment_id)
                log(
                    db,
                    fo,
                    audit.Step.SHOPIFY_SYNCED,
                    "Shopify fulfillment cancelled",
                    shipment_id=shipment.id,
                    actor=actor,
                )
            except Exception as exc:
                shipment.shopify_sync_error = f"Fulfillment cancel failed: {exc}"
                alerts.raise_alert(
                    db,
                    kind=alerts.AlertKind.SHOPIFY_SYNC_FAILED,
                    severity=AlertSeverity.ERROR,
                    title="Shopify fulfillment not cancelled",
                    fingerprint=f"SHOPIFY_FULFILLMENT_CANCEL:{shipment.shop_id}",
                    detail=f"{shipment.carrier_code} AWB {shipment.awb} was cancelled with the "
                    f"carrier, but its Shopify fulfillment was not ({exc}). Cancel it in Shopify.",
                    shop_id=shipment.shop_id,
                    order_id=shipment.order_id,
                    fulfillment_order_id=fo.id,
                    shipment_id=shipment.id,
                )
                log(
                    db,
                    fo,
                    audit.Step.SHOPIFY_SYNC_FAILED,
                    f"cancelled with carrier, but the Shopify fulfillment could not be cancelled ({exc}). "
                    "Cancel it manually in Shopify.",
                    level=LogLevel.ERROR,
                    shipment_id=shipment.id,
                    actor=actor,
                )

        fo.forced_carrier_code = None
        if fo.order.cancelled_at is not None:
            move(
                fo,
                LogisticsStatus.CANCELLED,
                "ORDER_CANCELLED",
                "Order cancelled; shipment cancelled with carrier",
            )
        elif reallocate:
            fo.selected_by = actor if actor.startswith("staff") else "system"
            move(
                fo,
                LogisticsStatus.AWAITING_ALLOCATION,
                "REALLOCATION_REQUESTED",
                "Shipment cancelled; re-allocating",
            )
            enqueue.enqueue_after_commit(db, enqueue.ALLOCATE, fo.id, f"cancel:{actor}")
        else:
            fo.selected_by = None
            fo.order.automation_disabled = True
            move(
                fo,
                LogisticsStatus.AUTOMATION_DISABLED,
                "SHIPMENT_CANCELLED",
                "Shipment cancelled by staff; automation paused for this order. Select a carrier to ship again.",
            )
        return StepResult("cancelled")


def staff_can_cancel(shipment: Shipment) -> None:
    if shipment.status not in CANCELLABLE and shipment.status != ShipmentStatus.CANCEL_REQUESTED:
        raise ConflictError(
            f"A {shipment.status.value} shipment cannot be cancelled (only before pickup)"
        )
    if shipment.status == ShipmentStatus.CREATION_UNKNOWN:
        raise ConflictError("Resolve the unknown creation outcome first")
