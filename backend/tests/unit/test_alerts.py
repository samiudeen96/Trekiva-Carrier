"""Alert rules, rendering, channels and the metrics format (no database)."""

from __future__ import annotations

from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any

import httpx
import pytest
import respx

from app.alerts.channels import AlertMessage, EmailChannel, SlackChannel, configured_channels
from app.alerts.dispatch import render_message, shopify_order_url
from app.alerts.service import CARRIER_AUTH_ERROR, AlertKind
from app.alerts.triggers import NEEDS_STAFF_REASON, fulfillment_order_alert, shipment_alert
from app.carriers.errors import CarrierAuthError
from app.core.config import Settings
from app.core.enums import AlertSeverity, LogisticsStatus, ShopifySyncStatus
from app.logistics.shipments import STAFF_RECONCILE
from app.ops.metrics import Metric, render

T0 = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 3, 9, 40, tzinfo=UTC)
SLACK_URL = "https://hooks.slack.com/services/T000/B000/secret"


# --- rules -------------------------------------------------------------------------------------


def test_constants_match_their_sources() -> None:
    assert NEEDS_STAFF_REASON == STAFF_RECONCILE
    assert CARRIER_AUTH_ERROR == CarrierAuthError.__name__


def test_failed_fulfillment_order_alerts() -> None:
    spec = fulfillment_order_alert(LogisticsStatus.FAILED, "MISSING_WEIGHT")
    assert spec is not None
    kind, severity, title = spec
    assert (kind, severity) == (AlertKind.SHIPMENT_FAILED, AlertSeverity.ERROR)
    assert "MISSING_WEIGHT" in title


def test_unknown_creation_needing_staff_is_critical() -> None:
    spec = fulfillment_order_alert(LogisticsStatus.RECONCILING, STAFF_RECONCILE)
    assert spec is not None and spec[:2] == (
        AlertKind.SHIPMENT_NEEDS_STAFF,
        AlertSeverity.CRITICAL,
    )


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (LogisticsStatus.MANUAL_REVIEW, "SHOPIFY_FULFILLMENT_HOLD"),  # normal Flow review
        (LogisticsStatus.NO_CARRIER_AVAILABLE, "NO_CARRIER"),  # reported on the order page
        (LogisticsStatus.RECONCILING, "CREATION_UNKNOWN"),  # still being reconciled
        (LogisticsStatus.SHIPPED, "SHIPMENT_CREATED"),
    ],
)
def test_normal_states_do_not_alert(status: LogisticsStatus, reason: str) -> None:
    assert fulfillment_order_alert(status, reason) is None


def test_only_failed_shopify_sync_alerts() -> None:
    assert shipment_alert(ShopifySyncStatus.FAILED) is not None
    assert shipment_alert(ShopifySyncStatus.PENDING) is None
    assert shipment_alert(ShopifySyncStatus.SYNCED) is None


def test_severity_ranks() -> None:
    assert AlertSeverity.WARNING.rank < AlertSeverity.ERROR.rank < AlertSeverity.CRITICAL.rank


# --- rendering ---------------------------------------------------------------------------------


def test_single_alert_message() -> None:
    message = render_message(
        severity=AlertSeverity.ERROR,
        title="Order could not be shipped (MISSING_WEIGHT)",
        shop_domain="trekiva.myshopify.com",
        lines=["#1001: no product weight"],
        count=1,
        first_seen=T0,
        last_seen=T0,
    )
    assert message.subject == "Order could not be shipped (MISSING_WEIGHT)"
    assert "Store: trekiva.myshopify.com" in message.body
    assert "- #1001: no product weight" in message.body
    assert "occurrences" not in message.body


def test_grouped_message_dedupes_and_truncates() -> None:
    lines = [f"#{1000 + i}: failed" for i in range(15)] + ["#1000: failed"]
    message = render_message(
        severity=AlertSeverity.ERROR,
        title="Order could not be shipped",
        shop_domain=None,
        lines=lines,
        count=16,
        first_seen=T0,
        last_seen=T1,
    )
    assert message.subject == "Order could not be shipped (16x)"
    assert message.body.count("- #") == 10
    assert "- ... and 5 more" in message.body
    assert "16 occurrences between 2026-10-03 09:00 and 2026-10-03 09:40 UTC." in message.body


def test_shopify_order_url() -> None:
    assert (
        shopify_order_url("trekiva.myshopify.com", "gid://shopify/Order/5550001")
        == "https://admin.shopify.com/store/trekiva/orders/5550001"
    )


# --- channels ----------------------------------------------------------------------------------

MESSAGE = AlertMessage(AlertSeverity.CRITICAL, "xpressbees rejected our credentials", "details")


@respx.mock
def test_slack_posts_text() -> None:
    route = respx.post(SLACK_URL).mock(return_value=httpx.Response(200, text="ok"))
    SlackChannel(SLACK_URL).send(MESSAGE)
    body = route.calls.last.request.content.decode()
    assert "xpressbees rejected our credentials" in body
    assert ":rotating_light:" in body


def test_slack_escapes_untrusted_text() -> None:
    message = AlertMessage(AlertSeverity.ERROR, "AT&T", "carrier said <!channel> <x|y>")
    text = SlackChannel(SLACK_URL).payload(message)["text"]
    assert "AT&amp;T" in text
    assert "&lt;!channel&gt; &lt;x|y&gt;" in text


@respx.mock
def test_slack_error_never_leaks_the_webhook_url() -> None:
    respx.post(SLACK_URL).mock(return_value=httpx.Response(404, text="no_service"))
    with pytest.raises(RuntimeError) as info:
        SlackChannel(SLACK_URL).send(MESSAGE)
    assert "404" in str(info.value)
    assert "secret" not in str(info.value)


class FakeSMTP:
    instances: list[FakeSMTP] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host, self.port = host, port
        self.calls: list[str] = []
        self.sent: list[EmailMessage] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *_: Any) -> None:
        self.calls.append("quit")

    def starttls(self, context: Any) -> None:
        self.calls.append("starttls")

    def login(self, username: str, password: str) -> None:
        self.calls.append(f"login:{username}")

    def send_message(self, message: EmailMessage) -> None:
        self.sent.append(message)


def test_email_uses_starttls_and_login(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSMTP.instances = []
    monkeypatch.setattr("app.alerts.channels.smtplib.SMTP", FakeSMTP)
    channel = EmailChannel(
        host="smtp.example.com",
        port=587,
        security="starttls",
        username="alerts@example.com",
        password="pw",
        sender="alerts@example.com",
        recipients=["ops@example.com", "owner@example.com"],
    )
    channel.send(MESSAGE)
    [smtp] = FakeSMTP.instances
    assert smtp.calls == ["starttls", "login:alerts@example.com", "quit"]
    [email] = smtp.sent
    assert email["Subject"] == "[Trekiva CRITICAL] xpressbees rejected our credentials"
    assert email["To"] == "ops@example.com, owner@example.com"


def test_channels_are_enabled_by_configuration() -> None:
    assert configured_channels(Settings(_env_file=None)) == []  # type: ignore[call-arg]
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        alert_slack_webhook_url=SLACK_URL,  # type: ignore[arg-type]
        alert_email_to="ops@example.com, owner@example.com",
        smtp_host="smtp.example.com",
    )
    assert [c.name for c in configured_channels(settings)] == ["slack", "email"]
    assert settings.alert_email_recipients == ["ops@example.com", "owner@example.com"]
    # Email needs both recipients and an SMTP host.
    no_host = Settings(_env_file=None, alert_email_to="ops@example.com")  # type: ignore[call-arg]
    assert configured_channels(no_host) == []


# --- metrics format ----------------------------------------------------------------------------


def test_prometheus_text_format() -> None:
    text = render(
        [
            Metric("trekiva_shipments", "Shipments by status.", [({"status": "DELIVERED"}, 3.0)]),
            Metric("trekiva_worker_heartbeat_age_seconds", "Age.", [({}, 12.5)]),
            Metric("trekiva_odd", "Escaping.", [({"q": 'a"b\\c'}, 1.0)]),
        ]
    )
    assert text.splitlines() == [
        "# HELP trekiva_shipments Shipments by status.",
        "# TYPE trekiva_shipments gauge",
        'trekiva_shipments{status="DELIVERED"} 3',
        "# HELP trekiva_worker_heartbeat_age_seconds Age.",
        "# TYPE trekiva_worker_heartbeat_age_seconds gauge",
        "trekiva_worker_heartbeat_age_seconds 12.5",
        "# HELP trekiva_odd Escaping.",
        "# TYPE trekiva_odd gauge",
        'trekiva_odd{q="a\\"b\\\\c"} 1',
    ]
    assert text.endswith("\n")
