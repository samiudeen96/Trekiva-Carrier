"""Shopify webhook topic handlers.

Handlers treat webhooks purely as *triggers*: they identify the affected order and then re-read
the live state from the GraphQL Admin API. Payload contents are never trusted as current state
(webhooks can arrive late, duplicated or out of order).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.carriers.types import TrackingUpdate
from app.core.enums import TrackingSource
from app.core.time import utcnow
from app.logistics.pipeline import sync_order
from app.models import Shipment, Shop, WebhookEvent
from app.services import audit, orders, shops
from app.shopify.api import order_gid
from app.tracking.service import record_updates

AdminFactory = shops.AdminFactory

_FO_GID_RE = re.compile(r"^gid://shopify/FulfillmentOrder/\d+$")


@dataclass(frozen=True)
class HandlerResult:
    ignored: bool = False
    note: str | None = None


def _walk_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _walk_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_strings(v)


def fulfillment_order_gids(payload: dict[str, Any]) -> list[str]:
    """Every fulfillment-order GID mentioned anywhere in a fulfillment_orders/* payload (the
    payload shapes differ per topic: split, merged, moved, hold...)."""
    seen: dict[str, None] = {}
    for s in _walk_strings(payload):
        if _FO_GID_RE.match(s):
            seen[s] = None
    return list(seen)


def _sync(
    db: Session, shop: Shop, admin_factory: AdminFactory, gid: str, event: WebhookEvent
) -> HandlerResult:
    sync_order(db, shop, admin_factory(db, shop), gid, trigger=f"webhook:{event.topic}")
    return HandlerResult()


def handle_order_topic(
    db: Session, shop: Shop, event: WebhookEvent, admin_factory: AdminFactory
) -> HandlerResult:
    payload = event.payload
    gid = payload.get("admin_graphql_api_id") or (
        order_gid(payload["id"]) if payload.get("id") else None
    )
    if not gid:
        return HandlerResult(ignored=True, note="No order id in payload")
    return _sync(db, shop, admin_factory, str(gid), event)


def handle_fulfillment_topic(
    db: Session, shop: Shop, event: WebhookEvent, admin_factory: AdminFactory
) -> HandlerResult:
    """fulfillments/create|update carry the numeric order id."""
    order_id = event.payload.get("order_id")
    if not order_id:
        return HandlerResult(ignored=True, note="No order_id in payload")
    return _sync(db, shop, admin_factory, order_gid(order_id), event)


def handle_fulfillment_order_topic(
    db: Session, shop: Shop, event: WebhookEvent, admin_factory: AdminFactory
) -> HandlerResult:
    fo_gids = fulfillment_order_gids(event.payload)
    if not fo_gids:
        return HandlerResult(ignored=True, note="No fulfillment order ids in payload")
    if event.topic == "fulfillment_orders/hold_released":
        orders.mark_hold_released(db, fo_gids, utcnow())

    order_gids = set(orders.order_gids_for_fulfillment_orders(db, fo_gids))
    admin = admin_factory(db, shop)
    if not order_gids:  # first time we hear of this order
        for fo_gid in fo_gids:
            parent = admin.fetch_order_id_for_fulfillment_order(fo_gid)
            if parent:
                order_gids.add(parent)
                break
    if not order_gids:
        return HandlerResult(ignored=True, note="Could not resolve order for fulfillment order")
    for gid in sorted(order_gids):
        sync_order(db, shop, admin, gid, trigger=f"webhook:{event.topic}")
    return HandlerResult()


def handle_app_uninstalled(
    db: Session, shop: Shop, event: WebhookEvent, admin_factory: AdminFactory
) -> HandlerResult:
    shops.mark_uninstalled(db, shop)
    return HandlerResult()


def handle_compliance(
    db: Session, shop: Shop, event: WebhookEvent, admin_factory: AdminFactory
) -> HandlerResult:
    audit.record(
        db,
        shop_id=shop.id,
        step=audit.Step.WEBHOOK_RECEIVED,
        message=f"Compliance webhook {event.topic} received; handle per privacy policy",
        data={"topic": event.topic},
    )
    return HandlerResult(note="compliance acknowledged")


def handle_carrier_tracking(
    db: Session, shop: Shop, event: WebhookEvent, admin_factory: AdminFactory
) -> HandlerResult:
    """Apply normalised tracking updates stored by the carrier webhook ingress."""
    updates = [TrackingUpdate.model_validate(u) for u in event.payload.get("updates") or []]
    if not updates:
        return HandlerResult(ignored=True, note="No tracking updates in payload")
    by_awb: dict[str, list[TrackingUpdate]] = {}
    for update in updates:
        by_awb.setdefault(update.awb, []).append(update)
    unknown: list[str] = []
    for awb, items in by_awb.items():
        shipment = db.scalar(
            select(Shipment)
            .where(
                Shipment.shop_id == shop.id,
                Shipment.carrier_code == event.source,
                Shipment.awb == awb,
            )
            .with_for_update()
        )
        if shipment is None:
            unknown.append(awb)
            continue
        record_updates(db, shipment, items, TrackingSource.WEBHOOK)
    if len(unknown) == len(by_awb):
        return HandlerResult(ignored=True, note=f"Unknown AWB(s): {', '.join(unknown)}")
    return HandlerResult(note=f"Unknown AWB(s): {', '.join(unknown)}" if unknown else None)


Handler = Callable[[Session, Shop, WebhookEvent, AdminFactory], HandlerResult]

HANDLERS: dict[str, Handler] = {
    "orders/create": handle_order_topic,
    "orders/updated": handle_order_topic,
    "orders/cancelled": handle_order_topic,
    "fulfillments/create": handle_fulfillment_topic,
    "fulfillments/update": handle_fulfillment_topic,
    "fulfillment_orders/placed_on_hold": handle_fulfillment_order_topic,
    "fulfillment_orders/hold_released": handle_fulfillment_order_topic,
    "fulfillment_orders/cancelled": handle_fulfillment_order_topic,
    "fulfillment_orders/split": handle_fulfillment_order_topic,
    "fulfillment_orders/merged": handle_fulfillment_order_topic,
    "fulfillment_orders/moved": handle_fulfillment_order_topic,
    "fulfillment_orders/order_routing_complete": handle_fulfillment_order_topic,
    "fulfillment_orders/rescheduled": handle_fulfillment_order_topic,
    "fulfillment_orders/scheduled_fulfillment_order_ready": handle_fulfillment_order_topic,
    "app/uninstalled": handle_app_uninstalled,
    "customers/data_request": handle_compliance,
    "customers/redact": handle_compliance,
    "shop/redact": handle_compliance,
}


default_admin_factory = shops.default_admin_factory
