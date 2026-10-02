"""A small, safe JSON condition language for allocation rules (no eval).

    {}                                                       -> always matches
    {"field": "payment_mode", "op": "eq", "value": "COD"}    -> leaf
    {"all": [cond, ...]}  {"any": [cond, ...]}  {"not": cond}

Fields: see `FIELDS`. String comparisons are case-insensitive.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from app.logistics.allocation.types import AllocationContext, RuleConfig

FIELDS: dict[str, str] = {
    "payment_mode": "string",
    "order_value": "number",
    "cod_amount": "number",
    "weight_g": "number",
    "warehouse_id": "number",
    "destination_pincode": "string",
    "destination_state": "string",
    "destination_country": "string",
    "skus": "list",
    "tags": "list",
}

STRING_OPS = {"eq", "ne", "in", "not_in", "starts_with"}
NUMBER_OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in"}
LIST_OPS = {"contains", "contains_any", "not_contains"}


class RuleError(ValueError):
    pass


def _fold(value: Any) -> Any:
    return value.casefold() if isinstance(value, str) else value


def _num(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RuleError(f"Not a number: {value!r}") from exc


def validate_conditions(cond: Any, *, _depth: int = 0) -> None:
    """Raise RuleError if `cond` is not a valid condition tree."""
    if _depth > 10:
        raise RuleError("Conditions nested too deeply")
    if not isinstance(cond, dict):
        raise RuleError("Condition must be an object")
    if not cond:
        return
    if "all" in cond or "any" in cond:
        key = "all" if "all" in cond else "any"
        if len(cond) != 1 or not isinstance(cond[key], list) or not cond[key]:
            raise RuleError(f"'{key}' must be the only key and a non-empty list")
        for child in cond[key]:
            validate_conditions(child, _depth=_depth + 1)
        return
    if "not" in cond:
        if len(cond) != 1:
            raise RuleError("'not' must be the only key")
        validate_conditions(cond["not"], _depth=_depth + 1)
        return
    if set(cond) != {"field", "op", "value"}:
        raise RuleError("Leaf condition needs exactly: field, op, value")
    kind = FIELDS.get(cond["field"])
    if kind is None:
        raise RuleError(f"Unknown field {cond['field']!r}")
    allowed = {"string": STRING_OPS, "number": NUMBER_OPS, "list": LIST_OPS}[kind]
    if cond["op"] not in allowed:
        raise RuleError(f"Operator {cond['op']!r} not valid for {cond['field']}")
    if cond["op"] in ("in", "not_in", "contains_any") and not isinstance(cond["value"], list):
        raise RuleError(f"Operator {cond['op']!r} needs a list value")
    if kind == "number":
        values = cond["value"] if isinstance(cond["value"], list) else [cond["value"]]
        for v in values:
            _num(v)


def matches(cond: dict[str, Any], ctx: AllocationContext) -> bool:
    if not cond:
        return True
    if "all" in cond:
        return all(matches(c, ctx) for c in cond["all"])
    if "any" in cond:
        return any(matches(c, ctx) for c in cond["any"])
    if "not" in cond:
        return not matches(cond["not"], ctx)
    return _leaf(cond["field"], cond["op"], cond["value"], ctx)


def _leaf(field_name: str, op: str, expected: Any, ctx: AllocationContext) -> bool:
    kind = FIELDS[field_name]
    actual = ctx.field_value(field_name)
    if kind == "list":
        have = {_fold(v) for v in actual}
        if op == "contains":
            return _fold(expected) in have
        if op == "not_contains":
            return _fold(expected) not in have
        return bool(have & {_fold(v) for v in expected})  # contains_any
    if actual is None:
        return op in ("ne", "not_in")
    if kind == "number":
        a = _num(actual)
        num_ops: dict[str, Callable[[], bool]] = {
            "eq": lambda: a == _num(expected),
            "ne": lambda: a != _num(expected),
            "gt": lambda: a > _num(expected),
            "gte": lambda: a >= _num(expected),
            "lt": lambda: a < _num(expected),
            "lte": lambda: a <= _num(expected),
            "in": lambda: a in {_num(v) for v in expected},
            "not_in": lambda: a not in {_num(v) for v in expected},
        }
        return num_ops[op]()
    a_str = _fold(str(actual))
    str_ops: dict[str, Callable[[], bool]] = {
        "eq": lambda: a_str == _fold(str(expected)),
        "ne": lambda: a_str != _fold(str(expected)),
        "in": lambda: a_str in {_fold(str(v)) for v in expected},
        "not_in": lambda: a_str not in {_fold(str(v)) for v in expected},
        "starts_with": lambda: a_str.startswith(_fold(str(expected))),
    }
    return str_ops[op]()


def select_rule(rules: Sequence[RuleConfig], ctx: AllocationContext) -> RuleConfig | None:
    """Highest-priority (lowest number) rule whose conditions match."""
    for rule in sorted(rules, key=lambda r: (r.priority, r.rule_id or 0)):
        if matches(rule.conditions, ctx):
            return rule
    return None
