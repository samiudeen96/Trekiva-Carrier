"""Carrier allocation engine.

    prefilter()  - before calling any carrier API: enabled, warehouse, weight, payment settings
    allocate()   - after offers are collected: hard filters -> rule limits -> strategy ranking
                   -> fallback carrier

The engine is carrier-agnostic: it only sees `CarrierProfile`s and `CarrierOffer`s. Every
rejection carries a reason code and a human-readable detail for the audit log and admin UI.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.carriers.types import CarrierOffer
from app.core.enums import AllocationStrategy, PaymentMode
from app.logistics.allocation.rules import select_rule
from app.logistics.allocation.strategies import STRATEGIES
from app.logistics.allocation.types import (
    DEFAULT_RULE,
    AllocationContext,
    AllocationResult,
    Candidate,
    CarrierProfile,
    OfferFailure,
    OfferOutcome,
    Rejection,
    RuleConfig,
)


class Reason:
    CARRIER_DISABLED = "CARRIER_DISABLED"
    WAREHOUSE_NOT_SUPPORTED = "WAREHOUSE_NOT_SUPPORTED"
    WEIGHT_OUT_OF_RANGE = "WEIGHT_OUT_OF_RANGE"
    PAYMENT_MODE_DISABLED = "PAYMENT_MODE_DISABLED"
    NO_OFFER = "NO_OFFER"
    OFFER_FAILED = "OFFER_FAILED"
    NOT_SERVICEABLE = "NOT_SERVICEABLE"
    COD_NOT_AVAILABLE = "COD_NOT_AVAILABLE"
    PREPAID_NOT_AVAILABLE = "PREPAID_NOT_AVAILABLE"
    PICKUP_NOT_AVAILABLE = "PICKUP_NOT_AVAILABLE"
    CARRIER_MAX_COST_EXCEEDED = "CARRIER_MAX_COST_EXCEEDED"
    RULE_MAX_COST_EXCEEDED = "RULE_MAX_COST_EXCEEDED"
    RULE_MAX_EDD_EXCEEDED = "RULE_MAX_EDD_EXCEEDED"
    COST_UNKNOWN = "COST_UNKNOWN"
    EDD_UNKNOWN = "EDD_UNKNOWN"
    BELOW_MIN_PERFORMANCE = "BELOW_MIN_PERFORMANCE"
    NOT_IN_ALLOWED_CARRIERS = "NOT_IN_ALLOWED_CARRIERS"


def effective_days(offer: CarrierOffer, ctx: AllocationContext) -> int | None:
    """Delivery time in days: from the EDD date if given, else the carrier's transit days."""
    if offer.edd is not None:
        return max((offer.edd - ctx.today).days, 0)
    return offer.transit_days


def prefilter(
    ctx: AllocationContext, profiles: Mapping[str, CarrierProfile]
) -> tuple[list[str], list[Rejection]]:
    """Carriers worth asking for an offer, plus rejections for the rest (no API calls made)."""
    eligible: list[str] = []
    rejected: list[Rejection] = []
    for code in sorted(profiles):
        p = profiles[code]
        if not p.enabled:
            rejected.append(Rejection(code, Reason.CARRIER_DISABLED, "Carrier is disabled"))
        elif ctx.warehouse_id not in p.supported_warehouse_ids:
            rejected.append(
                Rejection(
                    code, Reason.WAREHOUSE_NOT_SUPPORTED, "Carrier not mapped to this warehouse"
                )
            )
        elif (p.min_weight_g is not None and ctx.weight_g < p.min_weight_g) or (
            p.max_weight_g is not None and ctx.weight_g > p.max_weight_g
        ):
            rejected.append(
                Rejection(
                    code,
                    Reason.WEIGHT_OUT_OF_RANGE,
                    f"Weight {ctx.weight_g}g outside {p.min_weight_g or 0}-{p.max_weight_g or '∞'}g",
                )
            )
        elif ctx.payment_mode == PaymentMode.COD and not p.cod_enabled:
            rejected.append(
                Rejection(code, Reason.PAYMENT_MODE_DISABLED, "COD disabled for carrier")
            )
        elif ctx.payment_mode == PaymentMode.PREPAID and not p.prepaid_enabled:
            rejected.append(
                Rejection(code, Reason.PAYMENT_MODE_DISABLED, "Prepaid disabled for carrier")
            )
        else:
            eligible.append(code)
    return eligible, rejected


def _hard_check(
    ctx: AllocationContext, p: CarrierProfile, outcome: OfferOutcome | None
) -> Rejection | None:
    code = p.carrier_code
    if outcome is None:
        return Rejection(code, Reason.NO_OFFER, "No offer collected")
    if isinstance(outcome, OfferFailure):
        return Rejection(code, Reason.OFFER_FAILED, f"{outcome.error_class}: {outcome.message}")
    offer = outcome
    if not offer.serviceable:
        return Rejection(code, Reason.NOT_SERVICEABLE, offer.reason or "Not serviceable", offer)
    if ctx.payment_mode == PaymentMode.COD and offer.cod_available is False:
        return Rejection(
            code, Reason.COD_NOT_AVAILABLE, "COD not available for this pincode", offer
        )
    if ctx.payment_mode == PaymentMode.PREPAID and offer.prepaid_available is False:
        return Rejection(code, Reason.PREPAID_NOT_AVAILABLE, "Prepaid not available", offer)
    if offer.pickup_available is False:
        return Rejection(code, Reason.PICKUP_NOT_AVAILABLE, "Pickup not available", offer)
    if p.max_shipping_cost is not None:
        if offer.cost is None:
            return Rejection(
                code, Reason.COST_UNKNOWN, "Carrier has a cost cap but returned no rate", offer
            )
        if offer.cost > p.max_shipping_cost:
            return Rejection(
                code,
                Reason.CARRIER_MAX_COST_EXCEEDED,
                f"Cost {offer.cost} exceeds carrier cap {p.max_shipping_cost}",
                offer,
            )
    return None


def _rule_check(rule: RuleConfig, cand: Candidate) -> Rejection | None:
    params = rule.params
    code, offer = cand.carrier_code, cand.offer
    if params.only_carriers and code not in params.only_carriers:
        return Rejection(
            code, Reason.NOT_IN_ALLOWED_CARRIERS, f"Rule '{rule.name}' excludes carrier", offer
        )
    if params.max_cost is not None:
        if offer.cost is None:
            return Rejection(
                code, Reason.COST_UNKNOWN, "Rule has a max cost but carrier returned no rate", offer
            )
        if offer.cost > params.max_cost:
            return Rejection(
                code,
                Reason.RULE_MAX_COST_EXCEEDED,
                f"Cost {offer.cost} > rule max {params.max_cost}",
                offer,
            )
    if params.max_edd_days is not None:
        if cand.days is None:
            return Rejection(
                code, Reason.EDD_UNKNOWN, "Rule has a max EDD but carrier returned none", offer
            )
        if cand.days > params.max_edd_days:
            return Rejection(
                code,
                Reason.RULE_MAX_EDD_EXCEEDED,
                f"{cand.days} days > rule max {params.max_edd_days}",
                offer,
            )
    if params.min_performance_score is not None:
        score = offer.performance_score or cand.profile.performance_score
        if score is None or score < params.min_performance_score:
            return Rejection(
                code,
                Reason.BELOW_MIN_PERFORMANCE,
                f"Performance {score} < {params.min_performance_score}",
                offer,
            )
    return None


def allocate(
    ctx: AllocationContext,
    profiles: Mapping[str, CarrierProfile],
    offers: Mapping[str, OfferOutcome],
    rules: Sequence[RuleConfig] = (),
    *,
    prefilter_rejections: Sequence[Rejection] = (),
) -> AllocationResult:
    rule = select_rule(rules, ctx) or DEFAULT_RULE
    rejected: list[Rejection] = list(prefilter_rejections)
    already_rejected = {r.carrier_code for r in rejected}

    hard_passed: list[Candidate] = []
    for code in sorted(profiles):
        if code in already_rejected:
            continue
        profile = profiles[code]
        outcome = offers.get(code)
        rejection = _hard_check(ctx, profile, outcome)
        if rejection:
            rejected.append(rejection)
            continue
        assert isinstance(outcome, CarrierOffer)
        hard_passed.append(Candidate(code, outcome, profile, effective_days(outcome, ctx)))

    eligible: list[Candidate] = []
    soft_rejected: list[Rejection] = []
    for cand in hard_passed:
        rejection = _rule_check(rule, cand)
        if rejection:
            soft_rejected.append(rejection)
        else:
            eligible.append(cand)

    strategy = STRATEGIES[rule.strategy]
    ranked = strategy(eligible, rule.params)
    used_fallback = False

    fallback = rule.params.fallback_carrier
    if not ranked and fallback:
        fb = next((c for c in hard_passed if c.carrier_code == fallback), None)
        if fb is not None:
            ranked = [fb]
            used_fallback = True
            soft_rejected = [r for r in soft_rejected if r.carrier_code != fallback]

    return AllocationResult(
        rule=rule,
        ranked=tuple(ranked),
        rejected=tuple(rejected + soft_rejected),
        used_fallback=used_fallback,
    )


__all__ = ["AllocationStrategy", "Reason", "allocate", "effective_days", "prefilter"]
