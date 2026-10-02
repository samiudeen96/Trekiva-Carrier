"""Payment-mode detection. Read-only: COD King remains responsible for OTP and COD fees."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from app.core.enums import PaymentMode


def detect_payment_mode(
    gateways: Iterable[str], outstanding: Decimal, cod_gateway_names: Iterable[str]
) -> PaymentMode:
    """COD if any payment gateway is a configured COD gateway; PREPAID if nothing is owed;
    otherwise UNKNOWN (money is owed but not through a COD gateway, so a human must decide)."""
    cod_names = {n.strip().casefold() for n in cod_gateway_names if n.strip()}
    if any(g.strip().casefold() in cod_names for g in gateways):
        return PaymentMode.COD
    if outstanding <= 0:
        return PaymentMode.PREPAID
    return PaymentMode.UNKNOWN


def cod_amount(payment_mode: PaymentMode, outstanding: Decimal) -> Decimal:
    """Amount the courier must collect: Shopify's outstanding total (includes COD King's fee)."""
    if payment_mode != PaymentMode.COD:
        return Decimal("0")
    return max(outstanding, Decimal("0"))
