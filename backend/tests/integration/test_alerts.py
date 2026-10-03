"""Phase 5 alerts and monitoring: alerts raised by the pipeline and the health checks, delivery
with grouping / cooldown / retries, and the metrics + worker health endpoints."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import alerts
from app.alerts.channels import AlertMessage
from app.alerts.dispatch import dispatch_pending
from app.core.config import get_settings
from app.core.enums import AlertSeverity, AlertStatus, LogisticsStatus, WebhookStatus
from app.core.time import utcnow
from app.logistics.shipments import STAFF_RECONCILE
from app.main import app
from app.models import Alert, Shop, ShopifyFulfillmentOrder, WebhookEvent
from app.ops import checks
from app.shopify.errors import ShopifyUserError
from tests.factories import FakeShopifyAdmin, fo_json
from tests.harness import Runner, TaskQueue, setup_logistics
from tests.integration.test_shipping_flow import place_order

pytestmark = pytest.mark.db


class FakeChannel:
    def __init__(self, name: str = "fake", *, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.sent: list[AlertMessage] = []

    def send(self, message: AlertMessage) -> None:
        if self.fail:
            raise RuntimeError("boom")
        self.sent.append(message)


@pytest.fixture
def admin() -> FakeShopifyAdmin:
    return FakeShopifyAdmin()


@pytest.fixture
def runner(task_queue: TaskQueue, admin: FakeShopifyAdmin) -> Runner:
    return Runner(task_queue, admin)


def all_alerts(db: Session) -> list[Alert]:
    db.expire_all()
    return list(db.scalars(select(Alert).order_by(Alert.id)))


def kinds(db: Session) -> list[str]:
    return [a.kind for a in all_alerts(db)]


def queue_alert(db: Session, shop: Shop, title: str = "Test", **kwargs: Any) -> Alert:
    alert = alerts.raise_alert(
        db,
        kind=kwargs.pop("kind", alerts.AlertKind.TEST),
        severity=kwargs.pop("severity", AlertSeverity.ERROR),
        title=title,
        shop_id=shop.id,
        **kwargs,
    )
    db.commit()
    return alert


# --- raised by the pipeline --------------------------------------------------------------------


def test_shipped_order_raises_no_alert(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    place_order(db, shop, admin)
    runner.drain()
    assert kinds(db) == []


def test_failed_orders_raise_one_alert_each_grouped_by_reason(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    for i in range(2):
        place_order(
            db,
            shop,
            admin,
            order_id=3000 + i,
            name=f"TR-{3000 + i}",
            fulfillment_orders=[fo_json(700 + i, weight_grams=None)],
        )
    runner.drain()
    rows = all_alerts(db)
    assert [a.kind for a in rows] == ["SHIPMENT_FAILED", "SHIPMENT_FAILED"]
    assert {a.fingerprint for a in rows} == {f"SHIPMENT_FAILED:{shop.id}:MISSING_WEIGHT"}
    assert all(a.fulfillment_order_id and a.order_id for a in rows)

    channel = FakeChannel()
    assert dispatch_pending(channels=[channel]) == {"sent": 2}
    [message] = channel.sent  # grouped into one message
    assert message.subject == "Order could not be shipped (MISSING_WEIGHT) (2x)"
    assert "TR-3000" in message.body and "TR-3001" in message.body
    assert "https://admin.shopify.com/store/trekiva-test/orders/3000" in message.body
    assert {a.status for a in all_alerts(db)} == {AlertStatus.SENT}


def test_carrier_auth_failure_alerts_without_failing_the_order(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop, scenarios={"xpressbees": {"scenarios": {"560001": "AUTH_FAILURE"}}})
    place_order(db, shop, admin)
    runner.drain()
    [alert] = all_alerts(db)  # Ekart shipped it: nothing else to report
    assert alert.kind == "CARRIER_AUTH_FAILED"
    assert alert.severity == AlertSeverity.CRITICAL
    assert alert.fingerprint == f"CARRIER_AUTH_FAILED:{shop.id}:xpressbees"
    assert alert.data["during"] == "serviceability check"


def test_shopify_rejection_alerts(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    admin.fulfillment_error = ShopifyUserError(
        "fulfillmentCreate failed: on hold", user_errors=[{"message": "on hold"}]
    )
    place_order(db, shop, admin)
    runner.drain()
    [alert] = all_alerts(db)
    assert alert.kind == "SHOPIFY_SYNC_FAILED"
    assert alert.shipment_id is not None
    assert "on hold" in alert.detail


def test_reconciliation_needing_staff_alerts_once(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    place_order(db, shop, admin, pincode="999020")
    runner.step(1)  # allocate only
    db.expire_all()
    fo = db.scalars(select(ShopifyFulfillmentOrder)).one()
    fo.logistics_status = LogisticsStatus.RECONCILING
    fo.status_reason = STAFF_RECONCILE
    fo.status_detail = "check the carrier panel"
    db.commit()
    fo.status_detail = "still unknown"  # same state: no second alert
    db.commit()
    [alert] = all_alerts(db)
    assert (alert.kind, alert.severity) == ("SHIPMENT_NEEDS_STAFF", AlertSeverity.CRITICAL)
    assert alert.detail == "check the carrier panel"


# --- delivery ----------------------------------------------------------------------------------


def test_cooldown_groups_later_alerts_into_one_summary(db: Session, shop: Shop) -> None:
    channel = FakeChannel()
    queue_alert(db, shop, fingerprint="fp")
    assert dispatch_pending(channels=[channel]) == {"sent": 1}

    queue_alert(db, shop, fingerprint="fp")
    queue_alert(db, shop, fingerprint="fp")
    assert dispatch_pending(channels=[channel]) == {"deferred": 2}
    assert len(channel.sent) == 1

    later = utcnow() + timedelta(minutes=get_settings().alert_cooldown_minutes + 1)
    assert dispatch_pending(channels=[channel], now=later) == {"sent": 2}
    assert [m.subject for m in channel.sent] == ["Test", "Test (2x)"]


def test_different_fingerprints_are_not_rate_limited_together(db: Session, shop: Shop) -> None:
    channel = FakeChannel()
    queue_alert(db, shop, fingerprint="a")
    dispatch_pending(channels=[channel])
    queue_alert(db, shop, fingerprint="b")
    assert dispatch_pending(channels=[channel]) == {"sent": 1}


def test_failed_channel_is_retried_alone(db: Session, shop: Shop) -> None:
    good, bad = FakeChannel("good"), FakeChannel("bad", fail=True)
    queue_alert(db, shop)
    assert dispatch_pending(channels=[good, bad]) == {"retry": 1}
    [alert] = all_alerts(db)
    assert alert.status == AlertStatus.PENDING
    assert alert.delivered_channels == ["good"]
    assert alert.attempts == 1 and "boom" in (alert.last_error or "")
    assert alert.next_attempt_at is not None and alert.sent_at is not None

    # Not due yet.
    assert dispatch_pending(channels=[good, bad]) == {}
    bad.fail = False
    assert dispatch_pending(channels=[good, bad], now=utcnow() + timedelta(hours=2)) == {
        "sent": 1
    }
    assert (len(good.sent), len(bad.sent)) == (1, 1)  # "good" was not sent twice
    assert all_alerts(db)[0].delivered_channels == ["bad", "good"]


def test_alert_fails_after_max_attempts(db: Session, shop: Shop) -> None:
    from app.alerts.dispatch import MAX_ATTEMPTS

    bad = FakeChannel(fail=True)
    queue_alert(db, shop)
    for i in range(MAX_ATTEMPTS):
        dispatch_pending(channels=[bad], now=utcnow() + timedelta(days=i + 1))
    assert all_alerts(db)[0].status == AlertStatus.FAILED


def test_without_channels_alerts_are_kept_but_skipped(db: Session, shop: Shop) -> None:
    queue_alert(db, shop)
    assert dispatch_pending(channels=[]) == {"skipped": 1}
    assert all_alerts(db)[0].status == AlertStatus.SKIPPED


def test_below_min_severity_is_skipped(
    db: Session, shop: Shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "alert_min_severity", "ERROR")
    channel = FakeChannel()
    queue_alert(db, shop, severity=AlertSeverity.WARNING, fingerprint="w")
    queue_alert(db, shop, severity=AlertSeverity.CRITICAL, fingerprint="c")
    assert dispatch_pending(channels=[channel]) == {"skipped": 1, "sent": 1}


def test_abandoned_sending_rows_are_retried(db: Session, shop: Shop) -> None:
    alert = queue_alert(db, shop)
    alert.status = AlertStatus.SENDING
    alert.claimed_at = utcnow() - timedelta(hours=1)
    db.commit()
    channel = FakeChannel()
    assert dispatch_pending(channels=[channel]) == {"sent": 1}


# --- health checks -----------------------------------------------------------------------------


def test_stuck_order_is_reported_once(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    place_order(db, shop, admin)  # AWAITING_ALLOCATION; the allocate task never runs
    later = utcnow() + timedelta(hours=1)
    assert checks.run_health_checks(now=later) == {"STUCK_ORDERS": 1}
    [alert] = all_alerts(db)
    assert "AWAITING_ALLOCATION: 1" in alert.detail and "TR-001500" in alert.detail
    assert checks.run_health_checks(now=later + timedelta(minutes=5)) == {}


def test_waiting_orders_are_not_stuck_while_automation_is_off(
    db: Session, shop: Shop, admin: FakeShopifyAdmin
) -> None:
    setup_logistics(db, shop)
    shop.automation_enabled = False
    db.commit()
    place_order(db, shop, admin)
    assert checks.run_health_checks(now=utcnow() + timedelta(hours=1)) == {}


def test_awb_not_sent_to_shopify_is_reported(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    place_order(db, shop, admin)
    runner.step(2)  # allocate + create; the Shopify sync task never runs
    assert checks.run_health_checks(now=utcnow() + timedelta(hours=1)) == {
        "SHOPIFY_SYNC_STALLED": 1
    }


def test_failed_webhooks_are_reported_once(db: Session, shop: Shop) -> None:
    db.add(
        WebhookEvent(
            source="shopify",
            topic="orders/create",
            external_event_id="evt-1",
            shop_domain=shop.shop_domain,
            payload_hash="x",
            payload={},
            processing_status=WebhookStatus.FAILED,
            received_at=utcnow() - timedelta(hours=1),
            error_message="boom",
        )
    )
    db.commit()
    assert checks.run_health_checks() == {"WEBHOOKS_FAILING": 1}
    assert checks.run_health_checks() == {}


def test_queue_backlog_alert_is_not_repeated_while_open(
    db: Session, shop: Shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(checks.heartbeat, "queue_lengths", lambda: {"default": 10_000})
    assert checks.run_health_checks() == {"QUEUE_BACKLOG": 1}
    assert checks.run_health_checks() == {}


# --- endpoints ---------------------------------------------------------------------------------


def test_metrics_endpoint_requires_its_token(
    db: Session, shop: Shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(app)
    assert client.get("/metrics").status_code == 404  # disabled without METRICS_TOKEN

    monkeypatch.setattr(get_settings(), "metrics_token", SecretStr("s3cret"))
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer nope"}).status_code == 401

    resp = client.get("/metrics", headers={"Authorization": "Bearer s3cret"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert 'trekiva_fulfillment_orders{status="SHIPPED"} 0' in resp.text
    assert "trekiva_stuck_fulfillment_orders 0" in resp.text
    assert "trekiva_redis_up 0" in resp.text  # tests run without Redis


def test_worker_health_fails_without_heartbeat() -> None:
    resp = TestClient(app).get("/healthz/worker")
    assert resp.status_code == 503
