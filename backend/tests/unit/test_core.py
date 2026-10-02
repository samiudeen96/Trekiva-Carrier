"""State machines, payment detection, crypto, retry policy."""

from __future__ import annotations

from decimal import Decimal

import pytest
from cryptography.fernet import Fernet

from app.core.crypto import Encryptor, mask_secret
from app.core.enums import LogisticsStatus as L
from app.core.enums import PaymentMode
from app.core.enums import ShipmentStatus as S
from app.core.errors import ConfigurationError, InvalidTransition
from app.logistics.payment import cod_amount, detect_payment_mode
from app.logistics.state import ALLOWED_TRANSITIONS, can_transition, ensure_transition
from app.tracking.transitions import can_apply_tracking
from app.workers.retry import backoff_seconds

COD_NAMES = ["Cash on Delivery (COD)"]


# --- payment --------------------------------------------------------------------------------


def test_cod_detected_from_gateway_case_insensitively() -> None:
    assert (
        detect_payment_mode(["cash on delivery (cod)"], Decimal("1049"), COD_NAMES)
        == PaymentMode.COD
    )


def test_prepaid_when_nothing_outstanding() -> None:
    assert detect_payment_mode(["razorpay"], Decimal("0"), COD_NAMES) == PaymentMode.PREPAID


def test_unknown_when_money_owed_without_cod_gateway() -> None:
    assert detect_payment_mode(["bank_deposit"], Decimal("500"), COD_NAMES) == PaymentMode.UNKNOWN


def test_cod_amount_uses_outstanding_including_cod_fee() -> None:
    assert cod_amount(PaymentMode.COD, Decimal("1049.00")) == Decimal("1049.00")
    assert cod_amount(PaymentMode.PREPAID, Decimal("1049.00")) == Decimal("0")


# --- fulfillment order state machine --------------------------------------------------------


def test_every_status_has_transition_rules() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(L)


def test_cancelled_is_terminal() -> None:
    assert ALLOWED_TRANSITIONS[L.CANCELLED] == frozenset()
    with pytest.raises(InvalidTransition):
        ensure_transition(L.CANCELLED, L.AWAITING_ALLOCATION)


def test_review_and_release_path() -> None:
    assert can_transition(L.RECEIVED, L.MANUAL_REVIEW)
    assert can_transition(L.MANUAL_REVIEW, L.AWAITING_ALLOCATION)
    assert can_transition(L.AWAITING_ALLOCATION, L.MANUAL_REVIEW)


def test_cannot_jump_to_shipped_without_shipment_creation() -> None:
    assert not can_transition(L.AWAITING_ALLOCATION, L.SHIPPED)
    assert not can_transition(L.MANUAL_REVIEW, L.SHIPMENT_PENDING)


# --- shipment tracking progression ----------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "new", "applies"),
    [
        (S.AWB_CREATED, S.PICKED_UP, True),
        (S.IN_TRANSIT, S.PICKED_UP, False),  # stale, out of order
        (S.OUT_FOR_DELIVERY, S.DELIVERED, True),
        (S.DELIVERED, S.IN_TRANSIT, False),  # terminal
        (S.OUT_FOR_DELIVERY, S.DELIVERY_FAILED, True),
        (S.DELIVERY_FAILED, S.OUT_FOR_DELIVERY, True),  # re-attempt
        (S.DELIVERY_FAILED, S.RTO_INITIATED, True),
        (S.RTO_INITIATED, S.RTO_IN_TRANSIT, True),
        (S.RTO_IN_TRANSIT, S.RTO_INITIATED, False),
        (S.RTO_IN_TRANSIT, S.OUT_FOR_DELIVERY, False),
        (S.RTO_DELIVERED, S.RTO_IN_TRANSIT, False),
        (S.IN_TRANSIT, S.EXCEPTION, True),
        (S.EXCEPTION, S.IN_TRANSIT, True),
        (S.AWB_CREATED, S.CANCELLED, True),
        (S.IN_TRANSIT, S.CANCELLED, False),  # cannot cancel once picked up
        (S.CREATING, S.IN_TRANSIT, False),  # creation unconfirmed
        (S.CANCELLED, S.DELIVERED, False),
        (S.IN_TRANSIT, S.IN_TRANSIT, False),  # duplicate
    ],
)
def test_tracking_progression(current: S, new: S, applies: bool) -> None:
    assert can_apply_tracking(current, new) is applies


# --- crypto ---------------------------------------------------------------------------------


def test_encrypt_roundtrip_and_ciphertext_hides_secret() -> None:
    enc = Encryptor([Fernet.generate_key().decode()])
    token = enc.encrypt_json({"api_key": "super-secret-value"})
    assert b"super-secret-value" not in token
    assert enc.decrypt_json(token) == {"api_key": "super-secret-value"}


def test_key_rotation() -> None:
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    token = Encryptor([old]).encrypt("v")
    rotated = Encryptor([new, old])
    assert rotated.decrypt(token) == "v"
    assert Encryptor([new]).decrypt(rotated.rotate(token)) == "v"
    with pytest.raises(ConfigurationError):
        Encryptor([new]).decrypt(token)


def test_missing_keys_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError):
        Encryptor([])


def test_mask_secret() -> None:
    assert mask_secret("abcd1234wxyz") == "••••wxyz"
    assert mask_secret("short") == "••••"
    assert mask_secret(None) is None


# --- retry ----------------------------------------------------------------------------------


def test_backoff_grows_and_is_capped() -> None:
    assert 24 <= backoff_seconds(0) <= 36
    assert 48 <= backoff_seconds(1) <= 72
    assert backoff_seconds(20) <= 3600 * 1.2
