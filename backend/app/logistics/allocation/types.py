"""Inputs and outputs of the carrier allocation engine. No carrier-specific fields anywhere."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.carriers.types import CarrierOffer
from app.core.enums import AllocationStrategy, PaymentMode


@dataclass(frozen=True)
class AllocationContext:
    """Facts about the shipment being allocated (also the fields rule conditions can test)."""

    payment_mode: PaymentMode
    order_value: Decimal
    cod_amount: Decimal
    weight_g: int
    warehouse_id: int
    destination_pincode: str
    destination_state: str | None
    destination_country: str
    today: date
    skus: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    def field_value(self, name: str) -> Any:
        return getattr(self, name)


@dataclass(frozen=True)
class CarrierProfile:
    """A carrier's business settings (from carrier_settings + warehouse mappings)."""

    carrier_code: str
    enabled: bool
    cod_enabled: bool = True
    prepaid_enabled: bool = True
    priority: int = 100
    max_shipping_cost: Decimal | None = None
    min_weight_g: int | None = None
    max_weight_g: int | None = None
    performance_score: Decimal | None = None
    supported_warehouse_ids: frozenset[int] = frozenset()


@dataclass(frozen=True)
class OfferFailure:
    """A carrier call that failed during offer collection."""

    carrier_code: str
    error_class: str
    message: str
    retryable: bool


OfferOutcome = CarrierOffer | OfferFailure


class BalancedWeights(BaseModel):
    model_config = ConfigDict(frozen=True)

    speed: float = Field(default=0.4, ge=0)
    cost: float = Field(default=0.4, ge=0)
    performance: float = Field(default=0.1, ge=0)
    priority: float = Field(default=0.1, ge=0)


class StrategyParams(BaseModel):
    """Tunable parameters of an allocation rule (`allocation_rules.params`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_cost: Decimal | None = Field(default=None, ge=0)
    max_edd_days: int | None = Field(default=None, ge=0)
    min_performance_score: Decimal | None = Field(default=None, ge=0, le=100)
    fallback_carrier: str | None = None
    """Used when no carrier satisfies the rule's limits. It must still pass every hard check
    (serviceable, payment mode, weight, warehouse, carrier max cost)."""
    carrier_order: tuple[str, ...] = ()
    """PRIORITY: overrides carrier priorities. CUSTOM: the explicit preference order."""
    only_carriers: tuple[str, ...] = ()
    """CUSTOM: restrict to these carriers."""
    weights: BalancedWeights = BalancedWeights()


@dataclass(frozen=True)
class RuleConfig:
    strategy: AllocationStrategy
    params: StrategyParams = field(default_factory=StrategyParams)
    conditions: dict[str, Any] = field(default_factory=dict)
    rule_id: int | None = None
    name: str = "Default (fastest)"
    priority: int = 1_000_000


DEFAULT_RULE = RuleConfig(strategy=AllocationStrategy.FASTEST)


@dataclass(frozen=True)
class Candidate:
    """A carrier that passed every filter, with what the strategies rank on."""

    carrier_code: str
    offer: CarrierOffer
    profile: CarrierProfile
    days: int | None
    score: float | None = None


@dataclass(frozen=True)
class Rejection:
    carrier_code: str
    reason: str
    detail: str
    offer: CarrierOffer | None = None


@dataclass(frozen=True)
class AllocationResult:
    rule: RuleConfig
    ranked: tuple[Candidate, ...]
    rejected: tuple[Rejection, ...]
    used_fallback: bool = False

    @property
    def selected(self) -> Candidate | None:
        return self.ranked[0] if self.ranked else None

    def explain(self) -> str:
        if not self.ranked:
            reasons = "; ".join(f"{r.carrier_code}: {r.detail}" for r in self.rejected)
            return f"No carrier available ({reasons or 'no carriers configured'})"
        sel = self.ranked[0]
        how = (
            "fallback carrier"
            if self.used_fallback
            else f"{self.rule.strategy.value.lower()} strategy"
        )
        return (
            f"{sel.carrier_code} selected by {how} (rule '{self.rule.name}'): "
            f"cost={sel.offer.cost}, days={sel.days}"
        )
