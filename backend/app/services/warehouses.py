from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError
from app.models import Shop, Warehouse
from app.services import audit


def list_warehouses(db: Session, shop: Shop) -> list[Warehouse]:
    return list(
        db.scalars(select(Warehouse).where(Warehouse.shop_id == shop.id).order_by(Warehouse.id))
    )


def get_warehouse(db: Session, shop: Shop, warehouse_id: int) -> Warehouse:
    warehouse = db.get(Warehouse, warehouse_id)
    if warehouse is None or warehouse.shop_id != shop.id:
        raise NotFoundError("Warehouse not found")
    return warehouse


def save_warehouse(
    db: Session, shop: Shop, values: dict[str, Any], *, actor: str, warehouse_id: int | None = None
) -> Warehouse:
    warehouse = (
        get_warehouse(db, shop, warehouse_id) if warehouse_id else Warehouse(shop_id=shop.id)
    )
    for key, value in values.items():
        setattr(warehouse, key, value)
    if warehouse_id is None:
        db.add(warehouse)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError as exc:
        raise ConflictError(
            "A warehouse with this code or Shopify location already exists"
        ) from exc
    audit.record(
        db,
        shop_id=shop.id,
        step=audit.Step.SETTINGS_CHANGED,
        message=f"Warehouse {warehouse.code} saved",
        data={"warehouse_id": warehouse.id},
        actor=actor,
    )
    return warehouse
