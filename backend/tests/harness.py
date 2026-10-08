"""Run the background pipeline synchronously: drain captured tasks through the real logic."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.core.db import session_scope
from app.core.enums import CarrierEnvironment
from app.logistics.allocation_run import run_allocation
from app.logistics.pipeline import sync_order
from app.logistics.results import StepResult
from app.logistics.review_checks import place_review_hold
from app.logistics.shipments import cancel_shipment, create_shipment, reconcile_shipment
from app.logistics.shopify_sync import push_tracking, sync_to_shopify
from app.models import Shop, Warehouse
from app.services import carriers
from app.tracking.service import poll_shipment
from app.webhooks.processor import process_event
from app.workers import enqueue as q
from tests.factories import LOCATION_GID, FakeShopifyAdmin


class TaskQueue:
    """Captures enqueued Celery tasks instead of sending them to a broker."""

    def __init__(self) -> None:
        self.items: list[tuple[str, tuple[object, ...], float | None]] = []

    def __call__(self, task_name: str, *args: object, countdown: float | None = None) -> bool:
        self.items.append((task_name, args, countdown))
        return True

    def names(self) -> list[str]:
        return [name for name, _, _ in self.items]

    def pop_all(self) -> list[tuple[str, tuple[object, ...], float | None]]:
        items, self.items = self.items, []
        return items


class Runner:
    def __init__(self, queue: TaskQueue, admin: FakeShopifyAdmin) -> None:
        self.queue = queue
        self.admin = admin
        self.log: list[tuple[str, tuple[Any, ...], str]] = []

    def factory(self, db: Session, shop: Shop) -> FakeShopifyAdmin:
        return self.admin

    def _handlers(self) -> dict[str, Callable[..., Any]]:
        f = self.factory

        def order_sync(shop_id: int, gid: str, trigger: str) -> StepResult:
            with session_scope() as db:
                shop = db.get(Shop, shop_id)
                assert shop is not None
                sync_order(db, shop, self.admin, gid, trigger=trigger)  # type: ignore[arg-type]
            return StepResult("synced")

        def webhook(event_id: int) -> StepResult:
            res = process_event(event_id, admin_factory=f)  # type: ignore[arg-type]
            return StepResult(str(res.status), retry_in=1 if res.retry else None)

        return {
            q.ALLOCATE: lambda fo_id, trigger="queue": run_allocation(
                fo_id, trigger=trigger, admin_factory=f
            ),  # type: ignore[arg-type]
            q.CREATE_SHIPMENT: lambda fo_id: create_shipment(fo_id, admin_factory=f),  # type: ignore[arg-type]
            q.RECONCILE: reconcile_shipment,
            q.SYNC_SHOPIFY: lambda sid: sync_to_shopify(sid, admin_factory=f),  # type: ignore[arg-type]
            q.PUSH_TRACKING: lambda sid: push_tracking(sid, admin_factory=f),  # type: ignore[arg-type]
            q.CANCEL_SHIPMENT: lambda sid, reallocate, actor, reason: cancel_shipment(
                sid,
                reallocate=reallocate,
                actor=actor,
                reason=reason,
                admin_factory=f,  # type: ignore[arg-type]
            ),
            q.ORDER_SYNC: order_sync,
            q.WEBHOOK_PROCESS: webhook,
            q.POLL_TRACKING: poll_shipment,
            q.PLACE_REVIEW_HOLD: lambda fo_id: place_review_hold(fo_id, admin_factory=f),  # type: ignore[arg-type]
        }

    def drain(self, *, max_steps: int = 100, follow_retries: bool = True) -> list[tuple[str, str]]:
        handlers = self._handlers()
        retries: Counter[tuple[str, tuple[Any, ...]]] = Counter()
        done: list[tuple[str, str]] = []
        for _ in range(max_steps):
            if not self.queue.items:
                return done
            name, args, _countdown = self.queue.items.pop(0)
            result = handlers[name](*args)
            outcome = result.outcome if isinstance(result, StepResult) else str(result)
            done.append((name, outcome))
            self.log.append((name, args, outcome))
            if isinstance(result, StepResult) and result.retry_in is not None and follow_retries:
                retries[(name, args)] += 1
                if retries[(name, args)] <= 6:
                    self.queue.items.append((name, args, result.retry_in))
        raise AssertionError(f"queue did not drain: {self.queue.items}")

    def step(self, count: int = 1) -> list[tuple[str, str]]:
        """Run exactly the next `count` queued tasks (retries are not followed)."""
        handlers = self._handlers()
        done: list[tuple[str, str]] = []
        for _ in range(count):
            name, args, _countdown = self.queue.items.pop(0)
            result = handlers[name](*args)
            done.append((name, result.outcome if isinstance(result, StepResult) else str(result)))
        return done


def setup_logistics(
    db: Session, shop: Shop, *, scenarios: dict[str, dict[str, Any]] | None = None
) -> Warehouse:
    """A mapped warehouse plus both MOCK carriers enabled (optionally with scenario credentials)."""
    warehouse = Warehouse(
        shop_id=shop.id,
        code="BLR",
        name="Bengaluru WH",
        phone="+919811111111",
        address1="Plot 7",
        city="Bengaluru",
        state="Karnataka",
        pincode="560058",
        shopify_location_id=LOCATION_GID,
        default_package={"length_cm": 20, "breadth_cm": 15, "height_cm": 5},
    )
    db.add(warehouse)
    db.flush()
    for code in ("ekart", "xpressbees"):
        carriers.upsert_account(
            db,
            shop,
            code,
            environment=CarrierEnvironment.MOCK,
            label="default",
            credentials=(scenarios or {}).get(code, {}),
            is_active=True,
            make_active=True,
            actor="test",
        )
        carriers.set_mappings(
            db,
            shop,
            code,
            [{"warehouse_id": warehouse.id, "carrier_warehouse_ref": f"{code}-BLR"}],
            actor="test",
        )
        carriers.update_setting(db, shop, code, {"enabled": True}, actor="test")
    db.commit()
    return warehouse
