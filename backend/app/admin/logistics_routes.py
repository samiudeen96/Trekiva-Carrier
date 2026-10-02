"""Admin API: orders, shipments, tracking, logs and staff overrides."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.admin.deps import AdminContext, admin_context
from app.carriers.types import TrackingUpdate
from app.core.config import get_settings
from app.core.db import get_db
from app.core.enums import CarrierEnvironment, ShopifySyncStatus, TrackingSource, TrackingStatus
from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.core.time import utcnow
from app.logistics import shipments as shipment_ops
from app.logistics import shopify_sync
from app.logistics.allocation_run import preview_allocation
from app.logistics.plan import PlanError
from app.models import CarrierAccount, Shipment
from app.services import carriers, overrides, views
from app.tracking import service as tracking

router = APIRouter(prefix="/api/admin", tags=["admin: logistics"])


# --- reads ---------------------------------------------------------------------------------


@router.get("/orders")
def list_orders(
    status: str | None = None,
    carrier: str | None = None,
    q: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    ctx: AdminContext = Depends(admin_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return views.list_orders(
        db, ctx.shop, status=status, carrier=carrier, q=q, limit=limit, offset=offset
    )


@router.get("/orders/{order_id}")
def get_order(
    order_id: int, ctx: AdminContext = Depends(admin_context), db: Session = Depends(get_db)
) -> dict[str, Any]:
    return views.order_detail(db, ctx.shop, order_id)


@router.get("/shipments")
def list_shipments(
    status: str | None = None,
    carrier: str | None = None,
    q: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    ctx: AdminContext = Depends(admin_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return views.list_shipments(
        db, ctx.shop, status=status, carrier=carrier, q=q, limit=limit, offset=offset
    )


@router.get("/tracking")
def list_tracking(
    shipment_id: int | None = None,
    limit: int = Query(100, ge=1, le=500),
    ctx: AdminContext = Depends(admin_context),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return views.tracking_events(db, ctx.shop, shipment_id=shipment_id, limit=limit)


@router.get("/logs")
def list_logs(
    order_id: int | None = None,
    level: str | None = None,
    step: str | None = None,
    q: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    ctx: AdminContext = Depends(admin_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return views.list_logs(
        db, ctx.shop, order_id=order_id, level=level, step=step, q=q, limit=limit, offset=offset
    )


# --- fulfillment-order actions -------------------------------------------------------------


class AllocateIn(BaseModel):
    carrier_code: str | None = Field(
        default=None, description="Force this carrier (manual selection)"
    )


@router.post("/fulfillment-orders/{fo_id}/preview-allocation")
def preview(
    fo_id: int, ctx: AdminContext = Depends(admin_context), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Re-run serviceability + allocation without changing anything (stores carrier responses)."""
    fo = overrides.get_fo(db, ctx.shop, fo_id)
    try:
        result = preview_allocation(db, ctx.shop, fo)
    except PlanError as exc:
        raise ValidationFailed(exc.detail, code=exc.reason.lower()) from exc
    db.commit()
    return result


@router.post("/fulfillment-orders/{fo_id}/allocate")
def allocate(
    fo_id: int,
    body: AllocateIn,
    ctx: AdminContext = Depends(admin_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Re-run allocation / retry a failed order / manually select a carrier."""
    fo = overrides.request_allocation(
        db, ctx.shop, fo_id, actor=ctx.actor, carrier_code=body.carrier_code
    )
    db.commit()
    return {"queued": True, "fulfillment_order_id": fo.id, "forced_carrier": fo.forced_carrier_code}


class AutomationIn(BaseModel):
    disabled: bool


@router.post("/orders/{order_id}/automation")
def set_automation(
    order_id: int,
    body: AutomationIn,
    ctx: AdminContext = Depends(admin_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    order = overrides.set_order_automation(
        db, ctx.shop, order_id, disabled=body.disabled, actor=ctx.actor
    )
    db.commit()
    return {"order_id": order.id, "automation_disabled": order.automation_disabled}


# --- shipment actions ----------------------------------------------------------------------


def _shipment(db: Session, ctx: AdminContext, shipment_id: int) -> Shipment:
    shipment = db.get(Shipment, shipment_id)
    if shipment is None or shipment.shop_id != ctx.shop.id:
        raise NotFoundError("Shipment not found")
    return shipment


class CancelIn(BaseModel):
    reallocate: bool = False
    reason: str = Field(default="Cancelled by staff", max_length=500)


@router.post("/shipments/{shipment_id}/cancel")
def cancel(
    shipment_id: int,
    body: CancelIn,
    ctx: AdminContext = Depends(admin_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    shipment_ops.staff_can_cancel(_shipment(db, ctx, shipment_id))
    db.commit()  # release the request session before the carrier call
    result = shipment_ops.cancel_shipment(
        shipment_id, reallocate=body.reallocate, actor=ctx.actor, reason=body.reason
    )
    if result.outcome not in ("cancelled", "already_cancelled"):
        raise ConflictError(f"Cancellation failed: {result.detail}")
    db.expire_all()
    return views.shipment_row(_shipment(db, ctx, shipment_id))


class ResolveIn(BaseModel):
    created: bool
    awb: str | None = Field(default=None, max_length=64)
    carrier_shipment_id: str | None = Field(default=None, max_length=128)


@router.post("/shipments/{shipment_id}/resolve")
def resolve(
    shipment_id: int,
    body: ResolveIn,
    ctx: AdminContext = Depends(admin_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Staff decision for a shipment whose creation outcome could not be confirmed."""
    _shipment(db, ctx, shipment_id)
    shipment = shipment_ops.resolve_unknown(
        db,
        ctx.shop,
        shipment_id,
        created=body.created,
        awb=body.awb,
        carrier_shipment_id=body.carrier_shipment_id,
        actor=ctx.actor,
    )
    db.commit()
    return views.shipment_row(shipment)


@router.post("/shipments/{shipment_id}/sync-shopify")
def retry_shopify_sync(
    shipment_id: int, ctx: AdminContext = Depends(admin_context), db: Session = Depends(get_db)
) -> dict[str, Any]:
    shipment = _shipment(db, ctx, shipment_id)
    if shipment.shopify_sync_status == ShopifySyncStatus.FAILED:
        shipment.shopify_sync_status = ShopifySyncStatus.PENDING
    db.commit()
    result = shopify_sync.sync_to_shopify(shipment_id)
    db.expire_all()
    return {
        "outcome": result.outcome,
        "detail": result.detail,
        "shipment": views.shipment_row(_shipment(db, ctx, shipment_id)),
    }


class SimulateIn(BaseModel):
    status: TrackingStatus
    location: str | None = None


@router.post("/shipments/{shipment_id}/simulate-tracking")
def simulate_tracking(
    shipment_id: int,
    body: SimulateIn,
    ctx: AdminContext = Depends(admin_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Development only: feed a tracking status to a MOCK-carrier shipment."""
    if get_settings().is_production:
        raise NotFoundError("Not available")
    shipment = shipment_ops.lock_shipment(db, _shipment(db, ctx, shipment_id).id)
    account = (
        db.get(CarrierAccount, shipment.carrier_account_id) if shipment.carrier_account_id else None
    )
    if account is None or account.environment != CarrierEnvironment.MOCK or not shipment.awb:
        raise ConflictError("Tracking can only be simulated for MOCK carrier shipments with an AWB")
    adapter = carriers.adapter_for_shipment(db, shipment)
    raw = next(
        (code for code, st in adapter.status_map.items() if st == body.status), body.status.value
    )
    applied = tracking.record_updates(
        db,
        shipment,
        [
            TrackingUpdate(
                awb=shipment.awb,
                raw_status=raw,
                status=adapter.normalize_status(raw),
                occurred_at=utcnow(),
                location=body.location,
                description="Simulated (mock carrier)",
            )
        ],
        TrackingSource.MANUAL,
        actor=ctx.actor,
    )
    db.commit()
    return {"applied": bool(applied), "shipment": views.shipment_row(shipment)}
