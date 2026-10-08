"""High-level Shopify operations used by the logistics engine.

Every GraphQL document lives in `app.shopify.queries`; this class is the only place that turns
them into typed operations, so tests can substitute a fake with the same methods.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.errors import NotFoundError
from app.schemas.shopify import OrderSnapshot
from app.shopify.client import ShopifyGraphQLClient, raise_on_user_errors
from app.shopify.parse import parse_order
from app.shopify.queries import (
    FULFILLMENT_CANCEL,
    FULFILLMENT_CREATE,
    FULFILLMENT_EVENT_CREATE,
    FULFILLMENT_ORDER_FULFILLMENTS,
    FULFILLMENT_ORDER_HOLD,
    FULFILLMENT_ORDER_PARENT,
    ORDER_FOR_LOGISTICS,
    TAGS_ADD,
)


class ShopifyAdmin:
    def __init__(self, client: ShopifyGraphQLClient) -> None:
        self.client = client

    def fetch_order(self, order_gid: str) -> OrderSnapshot:
        data = self.client.execute(ORDER_FOR_LOGISTICS, {"id": order_gid})
        if not data.get("order"):
            raise NotFoundError(f"Shopify order {order_gid} not found")
        return parse_order(data["order"])

    def fetch_order_id_for_fulfillment_order(self, fulfillment_order_gid: str) -> str | None:
        data = self.client.execute(FULFILLMENT_ORDER_PARENT, {"id": fulfillment_order_gid})
        node = data.get("fulfillmentOrder") or {}
        return (node.get("order") or {}).get("id")

    def find_fulfillment_by_tracking(
        self, fulfillment_order_gid: str, tracking_number: str
    ) -> str | None:
        """An existing (non-cancelled) fulfillment of this FO carrying our AWB, if any.

        Checked before creating a fulfillment so a retried sync never fulfills twice."""
        data = self.client.execute(FULFILLMENT_ORDER_FULFILLMENTS, {"id": fulfillment_order_gid})
        node = data.get("fulfillmentOrder") or {}
        for f in (node.get("fulfillments") or {}).get("nodes") or []:
            numbers = [t.get("number") for t in f.get("trackingInfo") or []]
            if tracking_number in numbers and f.get("status") != "CANCELLED":
                return str(f["id"])
        return None

    def create_fulfillment(
        self,
        fulfillment_order_gid: str,
        *,
        company: str,
        number: str,
        url: str | None,
        notify_customer: bool,
    ) -> str:
        tracking: dict[str, Any] = {"company": company, "number": number}
        if url:
            tracking["url"] = url
        data = self.client.execute(
            FULFILLMENT_CREATE,
            {
                "fulfillment": {
                    "lineItemsByFulfillmentOrder": [{"fulfillmentOrderId": fulfillment_order_gid}],
                    "trackingInfo": tracking,
                    "notifyCustomer": notify_customer,
                }
            },
        )
        payload = raise_on_user_errors(data.get("fulfillmentCreate"), "fulfillmentCreate")
        return str(payload["fulfillment"]["id"])

    def create_fulfillment_event(
        self,
        fulfillment_gid: str,
        *,
        status: str,
        happened_at: datetime,
        message: str | None = None,
        city: str | None = None,
        estimated_delivery_at: datetime | None = None,
    ) -> str:
        event: dict[str, Any] = {
            "fulfillmentId": fulfillment_gid,
            "status": status,
            "happenedAt": happened_at.isoformat(),
        }
        if message:
            event["message"] = message[:255]
        if city:
            event["city"] = city
        if estimated_delivery_at:
            event["estimatedDeliveryAt"] = estimated_delivery_at.isoformat()
        data = self.client.execute(FULFILLMENT_EVENT_CREATE, {"fulfillmentEvent": event})
        payload = raise_on_user_errors(data.get("fulfillmentEventCreate"), "fulfillmentEventCreate")
        return str(payload["fulfillmentEvent"]["id"])

    def cancel_fulfillment(self, fulfillment_gid: str) -> None:
        data = self.client.execute(FULFILLMENT_CANCEL, {"id": fulfillment_gid})
        raise_on_user_errors(data.get("fulfillmentCancel"), "fulfillmentCancel")

    def hold_fulfillment_order(self, fulfillment_order_gid: str, *, reason: str, notes: str) -> str:
        """Place a fulfillment hold. `reason` is a FulfillmentHoldReason (HIGH_RISK_OF_FRAUD,
        OTHER, ...). Trekiva only ever places holds; staff release them in Shopify."""
        data = self.client.execute(
            FULFILLMENT_ORDER_HOLD,
            {
                "id": fulfillment_order_gid,
                "fulfillmentHold": {"reason": reason, "reasonNotes": notes[:255]},
            },
        )
        payload = raise_on_user_errors(data.get("fulfillmentOrderHold"), "fulfillmentOrderHold")
        return str((payload.get("fulfillmentHold") or {}).get("id") or "")

    def add_tags(self, resource_gid: str, tags: list[str]) -> None:
        data = self.client.execute(TAGS_ADD, {"id": resource_gid, "tags": tags})
        raise_on_user_errors(data.get("tagsAdd"), "tagsAdd")


def order_gid(numeric_or_gid: str | int) -> str:
    value = str(numeric_or_gid)
    return value if value.startswith("gid://") else f"gid://shopify/Order/{value}"
