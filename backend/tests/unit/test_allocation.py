"""Carrier allocation engine: filters, strategies, rules and fallback. Carrier-agnostic."""

from __future__ import annotations

from decimal import Decimal

from app.core.enums import AllocationStrategy, PaymentMode
from app.logistics.allocation import (
    OfferFailure,
    Reason,
    RuleConfig,
    StrategyParams,
    allocate,
    prefilter,
)
from app.logistics.allocation.types import BalancedWeights
from tests.factories import ctx, offer, profile

EKART, XB = "ekart", "xpressbees"
PROFILES = {EKART: profile(EKART), XB: profile(XB)}


def rule(strategy: AllocationStrategy, **params: object) -> RuleConfig:
    return RuleConfig(
        strategy=strategy, params=StrategyParams.model_validate(params), name=strategy.value
    )


def reasons(result: object) -> dict[str, str]:
    return {r.carrier_code: r.reason for r in result.rejected}  # type: ignore[attr-defined]


# --- serviceability scenarios ---------------------------------------------------------------


def test_both_serviceable_fastest_selects_xpressbees() -> None:
    offers = {EKART: offer(EKART, days=3, cost="75"), XB: offer(XB, days=2, cost="90")}
    result = allocate(ctx(), PROFILES, offers)
    assert result.selected is not None and result.selected.carrier_code == XB
    assert [c.carrier_code for c in result.ranked] == [XB, EKART]
    assert result.rule.strategy == AllocationStrategy.FASTEST  # default


def test_only_ekart_serviceable() -> None:
    offers = {
        EKART: offer(EKART),
        XB: offer(XB, days=1, serviceable=False, reason="pincode not served"),
    }
    result = allocate(ctx(), PROFILES, offers)
    assert result.selected is not None and result.selected.carrier_code == EKART
    assert reasons(result) == {XB: Reason.NOT_SERVICEABLE}


def test_only_xpressbees_serviceable() -> None:
    offers = {EKART: offer(EKART, days=1, serviceable=False), XB: offer(XB, days=4)}
    result = allocate(ctx(), PROFILES, offers)
    assert result.selected is not None and result.selected.carrier_code == XB


def test_neither_serviceable() -> None:
    offers = {EKART: offer(EKART, serviceable=False), XB: offer(XB, serviceable=False)}
    result = allocate(ctx(), PROFILES, offers)
    assert result.selected is None
    assert set(reasons(result).values()) == {Reason.NOT_SERVICEABLE}
    assert "No carrier available" in result.explain()


def test_cheapest_selects_ekart() -> None:
    offers = {EKART: offer(EKART, days=3, cost="75"), XB: offer(XB, days=2, cost="90")}
    result = allocate(ctx(), PROFILES, offers, [rule(AllocationStrategy.CHEAPEST)])
    assert result.selected is not None and result.selected.carrier_code == EKART


def test_fastest_breaks_ties_on_cost_then_priority() -> None:
    offers = {EKART: offer(EKART, days=2, cost="80"), XB: offer(XB, days=2, cost="80")}
    profiles = {EKART: profile(EKART, priority=50), XB: profile(XB, priority=10)}
    result = allocate(ctx(), profiles, offers)
    assert result.selected is not None and result.selected.carrier_code == XB
    cheaper = {EKART: offer(EKART, days=2, cost="70"), XB: offer(XB, days=2, cost="80")}
    assert allocate(ctx(), profiles, cheaper).selected.carrier_code == EKART  # type: ignore[union-attr]


def test_transit_days_used_when_no_edd() -> None:
    offers = {
        EKART: offer(EKART, days=None, transit_days=1),
        XB: offer(XB, days=None, transit_days=3),
    }
    assert allocate(ctx(), PROFILES, offers).selected.carrier_code == EKART  # type: ignore[union-attr]


# --- payment mode ---------------------------------------------------------------------------


def test_cod_order_skips_carrier_without_cod() -> None:
    offers = {EKART: offer(EKART, days=3), XB: offer(XB, days=1, cod_available=False)}
    result = allocate(ctx(payment_mode=PaymentMode.COD), PROFILES, offers)
    assert result.selected.carrier_code == EKART  # type: ignore[union-attr]
    assert reasons(result)[XB] == Reason.COD_NOT_AVAILABLE


def test_cod_disabled_in_carrier_settings_is_prefiltered() -> None:
    profiles = {EKART: profile(EKART), XB: profile(XB, cod_enabled=False)}
    eligible, rejected = prefilter(ctx(payment_mode=PaymentMode.COD), profiles)
    assert eligible == [EKART]
    assert rejected[0].reason == Reason.PAYMENT_MODE_DISABLED


# --- prefilter ------------------------------------------------------------------------------


def test_prefilter_rejects_disabled_unmapped_and_overweight() -> None:
    profiles = {
        "a": profile("a", enabled=False),
        "b": profile("b", supported_warehouse_ids=frozenset({2})),
        "c": profile("c", max_weight_g=400),
        "d": profile("d"),
    }
    eligible, rejected = prefilter(ctx(weight_g=500), profiles)
    assert eligible == ["d"]
    assert {r.carrier_code: r.reason for r in rejected} == {
        "a": Reason.CARRIER_DISABLED,
        "b": Reason.WAREHOUSE_NOT_SUPPORTED,
        "c": Reason.WEIGHT_OUT_OF_RANGE,
    }


def test_prefilter_rejections_are_kept_in_result() -> None:
    profiles = {EKART: profile(EKART, enabled=False), XB: profile(XB)}
    _, rejected = prefilter(ctx(), profiles)
    result = allocate(ctx(), profiles, {XB: offer(XB)}, prefilter_rejections=rejected)
    assert result.selected.carrier_code == XB  # type: ignore[union-attr]
    assert reasons(result) == {EKART: Reason.CARRIER_DISABLED}


# --- failures -------------------------------------------------------------------------------


def test_carrier_timeout_or_500_excludes_only_that_carrier() -> None:
    offers = {
        EKART: OfferFailure(EKART, "CarrierTransientError", "HTTP 500", True),
        XB: offer(XB, days=3),
    }
    result = allocate(ctx(), PROFILES, offers)
    assert result.selected.carrier_code == XB  # type: ignore[union-attr]
    assert reasons(result)[EKART] == Reason.OFFER_FAILED


def test_missing_offer_is_rejected() -> None:
    result = allocate(ctx(), PROFILES, {XB: offer(XB)})
    assert reasons(result)[EKART] == Reason.NO_OFFER


# --- limits and fallback --------------------------------------------------------------------


def test_carrier_max_cost_is_a_hard_limit() -> None:
    profiles = {EKART: profile(EKART, max_shipping_cost=Decimal("70")), XB: profile(XB)}
    offers = {EKART: offer(EKART, days=1, cost="75"), XB: offer(XB, days=3, cost="85")}
    result = allocate(ctx(), profiles, offers)
    assert result.selected.carrier_code == XB  # type: ignore[union-attr]
    assert reasons(result)[EKART] == Reason.CARRIER_MAX_COST_EXCEEDED


def test_rule_max_cost_and_max_edd() -> None:
    offers = {EKART: offer(EKART, days=3, cost="75"), XB: offer(XB, days=2, cost="90")}
    result = allocate(ctx(), PROFILES, offers, [rule(AllocationStrategy.FASTEST, max_cost="80")])
    assert result.selected.carrier_code == EKART  # type: ignore[union-attr]
    assert reasons(result)[XB] == Reason.RULE_MAX_COST_EXCEEDED

    result = allocate(ctx(), PROFILES, offers, [rule(AllocationStrategy.CHEAPEST, max_edd_days=2)])
    assert result.selected.carrier_code == XB  # type: ignore[union-attr]
    assert reasons(result)[EKART] == Reason.RULE_MAX_EDD_EXCEEDED


def test_fallback_carrier_used_when_rule_limits_exclude_everyone() -> None:
    offers = {EKART: offer(EKART, days=3, cost="75"), XB: offer(XB, days=2, cost="90")}
    result = allocate(
        ctx(),
        PROFILES,
        offers,
        [rule(AllocationStrategy.FASTEST, max_cost="50", fallback_carrier=EKART)],
    )
    assert result.used_fallback
    assert result.selected.carrier_code == EKART  # type: ignore[union-attr]
    assert "fallback" in result.explain()


def test_fallback_carrier_must_still_be_serviceable() -> None:
    offers = {EKART: offer(EKART, serviceable=False), XB: offer(XB, cost="90")}
    result = allocate(
        ctx(),
        PROFILES,
        offers,
        [rule(AllocationStrategy.FASTEST, max_cost="50", fallback_carrier=EKART)],
    )
    assert result.selected is None
    assert not result.used_fallback


def test_unknown_cost_rejected_when_rule_caps_cost() -> None:
    offers = {EKART: offer(EKART, cost=None), XB: offer(XB, cost="60")}
    result = allocate(ctx(), PROFILES, offers, [rule(AllocationStrategy.FASTEST, max_cost="80")])
    assert reasons(result)[EKART] == Reason.COST_UNKNOWN


# --- other strategies -----------------------------------------------------------------------


def test_priority_strategy_uses_carrier_priority() -> None:
    profiles = {EKART: profile(EKART, priority=1), XB: profile(XB, priority=5)}
    offers = {EKART: offer(EKART, days=5, cost="120"), XB: offer(XB, days=1, cost="50")}
    result = allocate(ctx(), profiles, offers, [rule(AllocationStrategy.PRIORITY)])
    assert result.selected.carrier_code == EKART  # type: ignore[union-attr]


def test_priority_strategy_carrier_order_overrides_settings() -> None:
    profiles = {EKART: profile(EKART, priority=1), XB: profile(XB, priority=5)}
    offers = {EKART: offer(EKART), XB: offer(XB)}
    result = allocate(
        ctx(), profiles, offers, [rule(AllocationStrategy.PRIORITY, carrier_order=[XB, EKART])]
    )
    assert result.selected.carrier_code == XB  # type: ignore[union-attr]


def test_custom_strategy_restricts_and_orders() -> None:
    offers = {EKART: offer(EKART, days=1), XB: offer(XB, days=5), "c3": offer("c3", days=0)}
    profiles = {**PROFILES, "c3": profile("c3")}
    custom = rule(AllocationStrategy.CUSTOM, carrier_order=[XB, EKART], only_carriers=[XB, EKART])
    result = allocate(ctx(), profiles, offers, [custom])
    assert [c.carrier_code for c in result.ranked] == [XB, EKART]
    assert reasons(result)["c3"] == Reason.NOT_IN_ALLOWED_CARRIERS


def test_balanced_weighs_speed_against_cost() -> None:
    offers = {EKART: offer(EKART, days=3, cost="75"), XB: offer(XB, days=2, cost="200")}
    cost_heavy = rule(
        AllocationStrategy.BALANCED,
        weights=BalancedWeights(speed=0.2, cost=0.8, performance=0, priority=0),
    )
    speed_heavy = rule(
        AllocationStrategy.BALANCED,
        weights=BalancedWeights(speed=0.8, cost=0.2, performance=0, priority=0),
    )
    assert allocate(ctx(), PROFILES, offers, [cost_heavy]).selected.carrier_code == EKART  # type: ignore[union-attr]
    assert allocate(ctx(), PROFILES, offers, [speed_heavy]).selected.carrier_code == XB  # type: ignore[union-attr]


def test_balanced_uses_performance_score() -> None:
    profiles = {
        EKART: profile(EKART, performance_score=Decimal("95")),
        XB: profile(XB, performance_score=Decimal("60")),
    }
    offers = {EKART: offer(EKART, days=2, cost="80"), XB: offer(XB, days=2, cost="80")}
    result = allocate(ctx(), profiles, offers, [rule(AllocationStrategy.BALANCED)])
    assert result.selected.carrier_code == EKART  # type: ignore[union-attr]
    assert result.ranked[0].score is not None and result.ranked[0].score < result.ranked[1].score  # type: ignore[operator]


def test_min_performance_score_filter() -> None:
    profiles = {
        EKART: profile(EKART, performance_score=Decimal("95")),
        XB: profile(XB, performance_score=Decimal("60")),
    }
    offers = {EKART: offer(EKART, days=5), XB: offer(XB, days=1)}
    result = allocate(
        ctx(), profiles, offers, [rule(AllocationStrategy.FASTEST, min_performance_score=80)]
    )
    assert result.selected.carrier_code == EKART  # type: ignore[union-attr]
    assert reasons(result)[XB] == Reason.BELOW_MIN_PERFORMANCE


# --- rule selection -------------------------------------------------------------------------


def test_matching_rule_with_highest_priority_wins() -> None:
    offers = {EKART: offer(EKART, days=3, cost="75"), XB: offer(XB, days=2, cost="90")}
    cod_cheapest = RuleConfig(
        strategy=AllocationStrategy.CHEAPEST,
        conditions={"field": "payment_mode", "op": "eq", "value": "COD"},
        rule_id=1,
        name="COD cheapest",
        priority=10,
    )
    catch_all = RuleConfig(strategy=AllocationStrategy.FASTEST, rule_id=2, name="all", priority=100)
    cod = allocate(ctx(payment_mode=PaymentMode.COD), PROFILES, offers, [catch_all, cod_cheapest])
    prepaid = allocate(ctx(), PROFILES, offers, [catch_all, cod_cheapest])
    assert (cod.rule.name, cod.selected.carrier_code) == ("COD cheapest", EKART)  # type: ignore[union-attr]
    assert (prepaid.rule.name, prepaid.selected.carrier_code) == ("all", XB)  # type: ignore[union-attr]


def test_engine_has_no_carrier_specific_logic() -> None:
    """A brand-new carrier code is allocated exactly like the existing ones."""
    profiles = {**PROFILES, "carrier_three": profile("carrier_three")}
    offers = {
        EKART: offer(EKART, days=3),
        XB: offer(XB, days=2),
        "carrier_three": offer("carrier_three", days=1),
    }
    assert allocate(ctx(), profiles, offers).selected.carrier_code == "carrier_three"  # type: ignore[union-attr]
