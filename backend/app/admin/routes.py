"""Embedded admin API (`/api/admin/*`). All routes require a valid App Bridge session token."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.admin.deps import AdminContext, admin_context
from app.carriers import registry
from app.core.config import get_settings
from app.core.db import get_db
from app.core.enums import CarrierEnvironment
from app.models import CarrierAccount
from app.schemas import admin as s
from app.schemas.settings import ShopSettings
from app.services import audit, carriers, dashboard, review, rules, warehouses
from app.workers.enqueue import enqueue_order_sync

router = APIRouter(prefix="/api/admin", tags=["admin"])


# --- session & settings ---------------------------------------------------------------------


@router.get("/session", response_model=s.SessionOut)
def get_session(ctx: AdminContext = Depends(admin_context)) -> s.SessionOut:
    settings = get_settings()
    return s.SessionOut(
        shop_domain=ctx.shop.shop_domain,
        user_id=ctx.user_id,
        automation_enabled=ctx.shop.automation_enabled,
        app_env=settings.app_env,
        mock_carriers_enabled=settings.mock_carriers_enabled(),
        api_version=settings.shopify_api_version,
    )


@router.get("/dashboard")
def get_dashboard(
    ctx: AdminContext = Depends(admin_context), db: Session = Depends(get_db)
) -> dict[str, Any]:
    return dashboard.summary(db, ctx.shop)


@router.get("/settings", response_model=s.SettingsOut)
def get_settings_route(ctx: AdminContext = Depends(admin_context)) -> s.SettingsOut:
    return s.SettingsOut(
        automation_enabled=ctx.shop.automation_enabled,
        settings=ShopSettings.load(ctx.shop.settings),
    )


@router.put("/settings", response_model=s.SettingsOut)
def put_settings(
    body: s.SettingsIn, ctx: AdminContext = Depends(admin_context), db: Session = Depends(get_db)
) -> s.SettingsOut:
    shop = ctx.shop
    if body.settings is not None:
        shop.settings = body.settings.model_dump(mode="json")
        audit.record(
            db,
            shop_id=shop.id,
            step=audit.Step.SETTINGS_CHANGED,
            message="Automation settings updated",
            data=shop.settings,
            actor=ctx.actor,
        )
    if body.automation_enabled is not None and body.automation_enabled != shop.automation_enabled:
        shop.automation_enabled = body.automation_enabled
        step = (
            audit.Step.AUTOMATION_ENABLED
            if body.automation_enabled
            else audit.Step.AUTOMATION_DISABLED
        )
        audit.record(
            db,
            shop_id=shop.id,
            step=step,
            message=f"Automation {'enabled' if body.automation_enabled else 'disabled'} for shop",
            actor=ctx.actor,
        )
    db.commit()
    return s.SettingsOut(
        automation_enabled=shop.automation_enabled, settings=ShopSettings.load(shop.settings)
    )


# --- carriers -------------------------------------------------------------------------------


def _account_out(account: CarrierAccount) -> s.CarrierAccountOut:
    return s.CarrierAccountOut(
        id=account.id,
        environment=account.environment,
        label=account.label,
        credentials_hint=account.credentials_hint,
        is_active=account.is_active,
        webhook_path=f"/webhooks/carriers/{account.carrier_code}/{account.webhook_token}",
        updated_at=account.updated_at,
    )


def _carrier_out(db: Session, ctx: AdminContext, code: str) -> s.CarrierOut:
    desc = registry.describe(code)
    setting = carriers.get_setting(db, ctx.shop, code)
    active = carriers.active_account(db, ctx.shop, code)
    mappings = carriers.mappings_for_account(db, active.id) if active else []
    return s.CarrierOut(
        code=code,
        display_name=desc.display_name,
        has_real_adapter=desc.has_real_adapter,
        has_mock_adapter=desc.has_mock_adapter,
        implementation_status=(
            "ready"
            if desc.has_real_adapter and desc.capabilities.serviceability
            else "awaiting_api_docs"
        ),
        capabilities=desc.capabilities.model_dump(),
        credentials_schema=desc.credentials_json_schema,
        mock_credentials_schema=desc.mock_credentials_json_schema,
        settings=s.CarrierSettingsOut.model_validate(setting),
        accounts=[_account_out(a) for a in carriers.list_accounts(db, ctx.shop, code)],
        warehouse_mappings=[s.WarehouseMappingOut.model_validate(m) for m in mappings],
    )


@router.get("/carriers", response_model=list[s.CarrierOut])
def list_carriers(
    ctx: AdminContext = Depends(admin_context), db: Session = Depends(get_db)
) -> list[s.CarrierOut]:
    return [_carrier_out(db, ctx, code) for code in registry.registered_codes()]


@router.get("/carriers/{code}", response_model=s.CarrierOut)
def get_carrier(
    code: str, ctx: AdminContext = Depends(admin_context), db: Session = Depends(get_db)
) -> s.CarrierOut:
    return _carrier_out(db, ctx, code)


@router.put("/carriers/{code}/settings", response_model=s.CarrierOut)
def put_carrier_settings(
    code: str,
    body: s.CarrierSettingsIn,
    ctx: AdminContext = Depends(admin_context),
    db: Session = Depends(get_db),
) -> s.CarrierOut:
    carriers.update_setting(
        db, ctx.shop, code, body.model_dump(exclude_unset=True), actor=ctx.actor
    )
    db.commit()
    return _carrier_out(db, ctx, code)


@router.put("/carriers/{code}/account", response_model=s.CarrierOut)
def put_carrier_account(
    code: str,
    body: s.CarrierAccountIn,
    ctx: AdminContext = Depends(admin_context),
    db: Session = Depends(get_db),
) -> s.CarrierOut:
    carriers.upsert_account(
        db,
        ctx.shop,
        code,
        environment=CarrierEnvironment(body.environment),
        label=body.label,
        credentials=body.credentials,
        is_active=body.is_active,
        make_active=body.make_active,
        actor=ctx.actor,
    )
    db.commit()
    return _carrier_out(db, ctx, code)


@router.put("/carriers/{code}/warehouses", response_model=s.CarrierOut)
def put_carrier_warehouses(
    code: str,
    body: list[s.WarehouseMappingIn],
    ctx: AdminContext = Depends(admin_context),
    db: Session = Depends(get_db),
) -> s.CarrierOut:
    carriers.set_mappings(db, ctx.shop, code, [m.model_dump() for m in body], actor=ctx.actor)
    db.commit()
    return _carrier_out(db, ctx, code)


# --- warehouses -----------------------------------------------------------------------------


@router.get("/warehouses", response_model=list[s.WarehouseOut])
def list_warehouses(
    ctx: AdminContext = Depends(admin_context), db: Session = Depends(get_db)
) -> list[Any]:
    return list(warehouses.list_warehouses(db, ctx.shop))


@router.post("/warehouses", response_model=s.WarehouseOut, status_code=201)
def create_warehouse(
    body: s.WarehouseIn, ctx: AdminContext = Depends(admin_context), db: Session = Depends(get_db)
) -> Any:
    warehouse = warehouses.save_warehouse(
        db, ctx.shop, body.model_dump(mode="json"), actor=ctx.actor
    )
    db.commit()
    return warehouse


@router.put("/warehouses/{warehouse_id}", response_model=s.WarehouseOut)
def update_warehouse(
    warehouse_id: int,
    body: s.WarehouseIn,
    ctx: AdminContext = Depends(admin_context),
    db: Session = Depends(get_db),
) -> Any:
    warehouse = warehouses.save_warehouse(
        db, ctx.shop, body.model_dump(mode="json"), actor=ctx.actor, warehouse_id=warehouse_id
    )
    db.commit()
    return warehouse


# --- allocation rules -----------------------------------------------------------------------


@router.get("/allocation-rules", response_model=list[s.AllocationRuleOut])
def list_rules(
    ctx: AdminContext = Depends(admin_context), db: Session = Depends(get_db)
) -> list[Any]:
    return list(rules.list_rules(db, ctx.shop))


@router.post("/allocation-rules", response_model=s.AllocationRuleOut, status_code=201)
def create_rule(
    body: s.AllocationRuleIn,
    ctx: AdminContext = Depends(admin_context),
    db: Session = Depends(get_db),
) -> Any:
    rule = rules.save_rule(db, ctx.shop, body.model_dump(mode="json"), actor=ctx.actor)
    db.commit()
    return rule


@router.put("/allocation-rules/{rule_id}", response_model=s.AllocationRuleOut)
def update_rule(
    rule_id: int,
    body: s.AllocationRuleIn,
    ctx: AdminContext = Depends(admin_context),
    db: Session = Depends(get_db),
) -> Any:
    rule = rules.save_rule(
        db, ctx.shop, body.model_dump(mode="json"), actor=ctx.actor, rule_id=rule_id
    )
    db.commit()
    return rule


@router.delete("/allocation-rules/{rule_id}", status_code=204)
def delete_rule(
    rule_id: int, ctx: AdminContext = Depends(admin_context), db: Session = Depends(get_db)
) -> Response:
    rules.delete_rule(db, ctx.shop, rule_id, actor=ctx.actor)
    db.commit()
    return Response(status_code=204)


# --- manual review --------------------------------------------------------------------------


@router.get("/manual-review")
def manual_review(
    ctx: AdminContext = Depends(admin_context), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    return review.list_queue(db, ctx.shop)


@router.post("/manual-review/{fulfillment_order_id}/approve")
def approve_review(
    fulfillment_order_id: int,
    ctx: AdminContext = Depends(admin_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    fo = review.approve(db, ctx.shop, fulfillment_order_id, actor=ctx.actor)
    order_gid = fo.order.shopify_order_id
    db.commit()  # commit before enqueuing so the worker sees the override
    queued = enqueue_order_sync(ctx.shop.id, order_gid, "staff:approve")
    return {"approved": True, "recheck_queued": queued}
