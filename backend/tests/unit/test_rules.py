from __future__ import annotations

import pytest

from app.core.enums import PaymentMode
from app.logistics.allocation.rules import RuleError, matches, validate_conditions
from tests.factories import ctx


@pytest.mark.parametrize(
    ("cond", "expected"),
    [
        ({}, True),
        ({"field": "payment_mode", "op": "eq", "value": "cod"}, True),
        ({"field": "order_value", "op": "gte", "value": 1000}, True),
        ({"field": "order_value", "op": "gt", "value": "1500"}, False),
        ({"field": "destination_pincode", "op": "starts_with", "value": "56"}, True),
        ({"field": "destination_state", "op": "in", "value": ["Karnataka", "Kerala"]}, True),
        ({"field": "destination_state", "op": "not_in", "value": ["karnataka"]}, False),
        ({"field": "tags", "op": "contains", "value": "vip"}, True),
        ({"field": "skus", "op": "contains_any", "value": ["X", "TRK-TEE-M"]}, True),
        ({"field": "skus", "op": "not_contains", "value": "TRK-TEE-M"}, False),
        (
            {
                "all": [
                    {"field": "payment_mode", "op": "eq", "value": "COD"},
                    {
                        "any": [
                            {"field": "weight_g", "op": "lt", "value": 100},
                            {"field": "weight_g", "op": "lte", "value": 500},
                        ]
                    },
                ]
            },
            True,
        ),
        ({"not": {"field": "payment_mode", "op": "eq", "value": "COD"}}, False),
    ],
)
def test_matches(cond: dict, expected: bool) -> None:  # type: ignore[type-arg]
    context = ctx(payment_mode=PaymentMode.COD, order_value="1200", tags=("VIP",))
    validate_conditions(cond)
    assert matches(cond, context) is expected


@pytest.mark.parametrize(
    "cond",
    [
        [],
        {"field": "nope", "op": "eq", "value": 1},
        {"field": "order_value", "op": "starts_with", "value": "1"},
        {"field": "order_value", "op": "gt", "value": "abc"},
        {"field": "payment_mode", "op": "eq"},
        {"field": "destination_state", "op": "in", "value": "Karnataka"},
        {"all": []},
        {"all": [{}], "any": [{}]},
        {"not": {}, "extra": 1},
    ],
)
def test_invalid_conditions_rejected(cond: object) -> None:
    with pytest.raises(RuleError):
        validate_conditions(cond)


def test_nesting_depth_limited() -> None:
    cond: dict = {"field": "weight_g", "op": "gt", "value": 1}  # type: ignore[type-arg]
    for _ in range(12):
        cond = {"not": cond}
    with pytest.raises(RuleError):
        validate_conditions(cond)
