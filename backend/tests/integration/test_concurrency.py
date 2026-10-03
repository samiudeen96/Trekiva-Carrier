"""Real concurrency (separate connections, real commits): two workers racing to create a
shipment for the same fulfillment order must produce exactly one carrier call and one AWB."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.carriers.mock import MockXpressBeesAdapter
from app.core import db as core_db
from app.core.crypto import get_encryptor
from app.core.enums import LogisticsStatus, ShipmentStatus
from app.core.time import utcnow
from app.logistics.pipeline import apply_order_snapshot
from app.logistics.shipments import create_shipment
from app.models import Shipment, Shop, ShopifyFulfillmentOrder
from app.schemas.settings import ShopSettings
from app.shopify.parse import parse_order
from tests.conftest import SHOP_DOMAIN
from tests.factories import FakeShopifyAdmin, order_json
from tests.harness import Runner, TaskQueue, setup_logistics

pytestmark = pytest.mark.db

TABLES = (
    "alerts, tracking_events, carrier_quotes, carrier_serviceability_checks, automation_logs, "
    "shipments, "
    "shopify_fulfillment_orders, shopify_orders, carrier_warehouse_mappings, carrier_settings, "
    "carrier_accounts, allocation_rules, warehouses, webhook_events, shops"
)


@pytest.fixture
def committed(engine) -> Iterator[None]:  # type: ignore[no-untyped-def]
    """Real sessions (no outer rollback); tables are truncated afterwards."""
    core_db.set_session_factory(None)
    try:
        yield
    finally:
        core_db.set_session_factory(None)
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))


def test_concurrent_create_produces_one_shipment(committed: None, task_queue: TaskQueue) -> None:
    admin = FakeShopifyAdmin()
    runner = Runner(task_queue, admin)
    with core_db.session_scope() as db:
        shop = Shop(
            shop_domain=SHOP_DOMAIN,
            access_token_enc=get_encryptor().encrypt("t"),
            automation_enabled=True,
            settings=ShopSettings().model_dump(mode="json"),
        )
        db.add(shop)
        db.flush()
        setup_logistics(db, shop)
        raw = order_json(created_at=utcnow() - timedelta(hours=1))
        admin.put(raw)
        apply_order_snapshot(db, shop, parse_order(raw), trigger="test")
    runner.step(1)  # allocation -> ALLOCATED
    with core_db.session_scope() as db:
        fo = db.scalar(select(ShopifyFulfillmentOrder))
        assert fo is not None and fo.logistics_status == LogisticsStatus.ALLOCATED
        fo_id = fo.id

    calls: list[str] = []
    original = MockXpressBeesAdapter.create_shipment

    def slow_create(self, request):  # type: ignore[no-untyped-def]
        calls.append(request.idempotency_key)
        time.sleep(0.3)  # widen the race window
        return original(self, request)

    MockXpressBeesAdapter.create_shipment = slow_create  # type: ignore[method-assign]
    results: list[str] = []
    try:
        barrier = threading.Barrier(2)

        def worker() -> None:
            barrier.wait()
            results.append(create_shipment(fo_id, admin_factory=runner.factory).outcome)  # type: ignore[arg-type]

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    finally:
        MockXpressBeesAdapter.create_shipment = original  # type: ignore[method-assign]

    assert sorted(results) == ["created", "not_allocated"]
    assert len(calls) == 1
    with core_db.session_scope() as db:
        rows = list(db.scalars(select(Shipment)))
        assert len(rows) == 1 and rows[0].status == ShipmentStatus.AWB_CREATED


def test_database_rejects_concurrent_active_inserts(committed: None, task_queue: TaskQueue) -> None:
    """Even without the row lock, the partial unique index stops a second active shipment."""
    admin = FakeShopifyAdmin()
    with core_db.session_scope() as db:
        shop = Shop(
            shop_domain=SHOP_DOMAIN,
            access_token_enc=get_encryptor().encrypt("t"),
            automation_enabled=True,
            settings=ShopSettings().model_dump(mode="json"),
        )
        db.add(shop)
        db.flush()
        raw = order_json(created_at=utcnow() - timedelta(hours=1))
        admin.put(raw)
        apply_order_snapshot(db, shop, parse_order(raw), trigger="test")
        fo = db.scalar(select(ShopifyFulfillmentOrder))
        assert fo is not None
        base = dict(
            shop_id=shop.id,
            order_id=fo.order_id,
            fulfillment_order_id=fo.id,
            shopify_order_id=raw["id"],
            shopify_order_name="TR-1",
            shopify_fulfillment_order_id=fo.shopify_fulfillment_order_id,
            status=ShipmentStatus.CREATING,
        )

    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def insert(carrier: str, key: str) -> None:
        barrier.wait()
        try:
            with core_db.session_scope() as db:
                db.add(Shipment(carrier_code=carrier, idempotency_key=key, **base))  # type: ignore[arg-type]
        except IntegrityError as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=insert, args=("ekart", "a")),
        threading.Thread(target=insert, args=("xpressbees", "b")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(errors) == 1
    with core_db.session_scope() as db:
        assert len(list(db.scalars(select(Shipment)))) == 1
