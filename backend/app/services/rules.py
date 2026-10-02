from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.carriers import registry
from app.core.enums import AllocationStrategy
from app.core.errors import NotFoundError, ValidationFailed
from app.logistics.allocation.rules import RuleError, validate_conditions
from app.logistics.allocation.types import RuleConfig, StrategyParams
from app.models import AllocationRule, Shop
from app.services import audit


def list_rules(db: Session, shop: Shop) -> list[AllocationRule]:
    return list(
        db.scalars(
            select(AllocationRule)
            .where(AllocationRule.shop_id == shop.id)
            .order_by(AllocationRule.priority, AllocationRule.id)
        )
    )


def _validate(
    strategy: AllocationStrategy, conditions: dict[str, Any], params: dict[str, Any]
) -> StrategyParams:
    try:
        validate_conditions(conditions)
    except RuleError as exc:
        raise ValidationFailed(f"Invalid conditions: {exc}") from exc
    try:
        parsed = StrategyParams.model_validate(params)
    except ValidationError as exc:
        raise ValidationFailed(f"Invalid params: {exc.errors(include_url=False)}") from exc
    known = set(registry.registered_codes())
    referenced = set(parsed.carrier_order) | set(parsed.only_carriers)
    if parsed.fallback_carrier:
        referenced.add(parsed.fallback_carrier)
    unknown = referenced - known
    if unknown:
        raise ValidationFailed(f"Unknown carriers: {', '.join(sorted(unknown))}")
    if strategy == AllocationStrategy.CUSTOM and not parsed.carrier_order:
        raise ValidationFailed("CUSTOM strategy needs params.carrier_order")
    return parsed


def save_rule(
    db: Session, shop: Shop, values: dict[str, Any], *, actor: str, rule_id: int | None = None
) -> AllocationRule:
    rule = get_rule(db, shop, rule_id) if rule_id else AllocationRule(shop_id=shop.id)
    merged = {
        "strategy": rule.strategy or AllocationStrategy.FASTEST,
        "conditions": rule.conditions or {},
        "params": rule.params or {},
        **values,
    }
    params = _validate(
        AllocationStrategy(merged["strategy"]), merged["conditions"], merged["params"]
    )
    for key, value in values.items():
        setattr(rule, key, value)
    rule.params = params.model_dump(mode="json", exclude_defaults=True)
    if rule_id is None:
        db.add(rule)
    db.flush()
    audit.record(
        db,
        shop_id=shop.id,
        step=audit.Step.SETTINGS_CHANGED,
        message=f"Allocation rule '{rule.name}' saved",
        data={"rule_id": rule.id, "strategy": str(rule.strategy)},
        actor=actor,
    )
    return rule


def get_rule(db: Session, shop: Shop, rule_id: int) -> AllocationRule:
    rule = db.get(AllocationRule, rule_id)
    if rule is None or rule.shop_id != shop.id:
        raise NotFoundError("Rule not found")
    return rule


def delete_rule(db: Session, shop: Shop, rule_id: int, *, actor: str) -> None:
    rule = get_rule(db, shop, rule_id)
    db.delete(rule)
    audit.record(
        db,
        shop_id=shop.id,
        step=audit.Step.SETTINGS_CHANGED,
        message=f"Allocation rule '{rule.name}' deleted",
        data={"rule_id": rule_id},
        actor=actor,
    )


def load_rule_configs(db: Session, shop: Shop) -> list[RuleConfig]:
    return [
        RuleConfig(
            strategy=AllocationStrategy(r.strategy),
            params=StrategyParams.model_validate(r.params),
            conditions=r.conditions,
            rule_id=r.id,
            name=r.name,
            priority=r.priority,
        )
        for r in list_rules(db, shop)
        if r.is_active
    ]
