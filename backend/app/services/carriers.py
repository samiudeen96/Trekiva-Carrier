"""Carrier configuration: settings, encrypted credentials, warehouse mappings, adapter building."""

from __future__ import annotations

import secrets
from typing import Any

from pydantic import BaseModel, SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.carriers import registry
from app.carriers.base import CarrierAdapter
from app.core.config import get_settings
from app.core.crypto import get_encryptor, mask_secret
from app.core.enums import CarrierEnvironment
from app.core.errors import NotFoundError, ValidationFailed
from app.logistics.allocation.types import CarrierProfile
from app.models import (
    CarrierAccount,
    CarrierSetting,
    CarrierWarehouseMapping,
    Shipment,
    Shop,
    Warehouse,
)
from app.services import audit

# --- credentials helpers --------------------------------------------------------------------


def reveal(value: Any) -> Any:
    """JSON-ready plain values of a credentials model (secrets revealed, for encryption only)."""
    if isinstance(value, BaseModel):
        return {k: reveal(getattr(value, k)) for k in type(value).model_fields}
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, dict):
        return {k: reveal(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [reveal(v) for v in value]
    if hasattr(value, "value") and isinstance(getattr(value, "value", None), str):
        return value.value  # enums
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def credential_hint(value: Any) -> Any:
    """Display-safe form: secrets masked, everything else as-is."""
    if isinstance(value, BaseModel):
        return {k: credential_hint(getattr(value, k)) for k in type(value).model_fields}
    if isinstance(value, SecretStr):
        return mask_secret(value.get_secret_value())
    if isinstance(value, dict):
        return {k: credential_hint(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [credential_hint(v) for v in value]
    return reveal(value)


def _drop_blank_secrets(schema: type[BaseModel], incoming: dict[str, Any]) -> dict[str, Any]:
    """A blank secret in an update means "keep the stored value"."""
    out = {}
    for key, value in incoming.items():
        is_secret = _is_secret_field(schema, key)
        if is_secret and value in (None, ""):
            continue
        if is_secret and isinstance(value, dict):
            value = {k: v for k, v in value.items() if v not in (None, "")}
        out[key] = value
    return out


def _is_secret_field(schema: type[BaseModel], key: str) -> bool:
    field = schema.model_fields.get(key)
    return field is not None and "SecretStr" in str(field.annotation)


def _merge(
    schema: type[BaseModel], existing: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    """Incoming values replace stored ones, except secret maps, which are merged key by key so
    that re-saving a form without re-typing every secret keeps the stored secrets."""
    merged = dict(existing)
    for key, value in incoming.items():
        if (
            _is_secret_field(schema, key)
            and isinstance(value, dict)
            and isinstance(merged.get(key), dict)
        ):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


# --- settings --------------------------------------------------------------------------------


def get_setting(db: Session, shop: Shop, code: str) -> CarrierSetting:
    registry.describe(code)  # raises NotFound for unknown carriers
    setting = db.scalar(
        select(CarrierSetting).where(
            CarrierSetting.shop_id == shop.id, CarrierSetting.carrier_code == code
        )
    )
    if setting is None:
        setting = CarrierSetting(
            shop_id=shop.id,
            carrier_code=code,
            enabled=False,
            cod_enabled=True,
            prepaid_enabled=True,
            priority=100,
        )
        db.add(setting)
        db.flush()
    return setting


def update_setting(
    db: Session, shop: Shop, code: str, changes: dict[str, Any], *, actor: str
) -> CarrierSetting:
    setting = get_setting(db, shop, code)
    if changes.get("enabled"):
        account = (
            db.get(CarrierAccount, setting.active_account_id) if setting.active_account_id else None
        )
        if account is None or not account.is_active:
            raise ValidationFailed(
                "Add and activate carrier credentials before enabling the carrier"
            )
    for key, value in changes.items():
        setattr(setting, key, value)
    db.flush()
    audit.record(
        db,
        shop_id=shop.id,
        step=audit.Step.CARRIER_CONFIG_CHANGED,
        message=f"{code} settings updated",
        data={"carrier": code, "changes": {k: str(v) for k, v in changes.items()}},
        actor=actor,
    )
    return setting


# --- accounts --------------------------------------------------------------------------------


def list_accounts(db: Session, shop: Shop, code: str) -> list[CarrierAccount]:
    return list(
        db.scalars(
            select(CarrierAccount)
            .where(CarrierAccount.shop_id == shop.id, CarrierAccount.carrier_code == code)
            .order_by(CarrierAccount.id)
        )
    )


def upsert_account(
    db: Session,
    shop: Shop,
    code: str,
    *,
    environment: CarrierEnvironment,
    label: str,
    credentials: dict[str, Any],
    is_active: bool,
    make_active: bool,
    actor: str,
) -> CarrierAccount:
    if environment == CarrierEnvironment.MOCK and not get_settings().mock_carriers_enabled():
        raise ValidationFailed("Mock carriers are disabled in this environment")
    cls = registry.get_adapter_class(code, environment)
    enc = get_encryptor()
    account = db.scalar(
        select(CarrierAccount).where(
            CarrierAccount.shop_id == shop.id,
            CarrierAccount.carrier_code == code,
            CarrierAccount.environment == environment,
            CarrierAccount.label == label,
        )
    )
    existing = (
        enc.decrypt_json(account.credentials_enc) if account and account.credentials_enc else {}
    )
    merged = _merge(
        cls.credentials_schema, existing, _drop_blank_secrets(cls.credentials_schema, credentials)
    )
    try:
        validated = cls.credentials_schema.model_validate(merged)
    except ValidationError as exc:
        raise ValidationFailed(f"Invalid credentials: {exc.errors(include_url=False)}") from exc

    if account is None:
        account = CarrierAccount(
            shop_id=shop.id,
            carrier_code=code,
            environment=environment,
            label=label,
            webhook_token=secrets.token_urlsafe(32),
        )
        db.add(account)
    account.credentials_enc = enc.encrypt_json(reveal(validated))
    account.credentials_hint = credential_hint(validated)
    account.is_active = is_active
    db.flush()

    setting = get_setting(db, shop, code)
    if make_active:
        setting.active_account_id = account.id
    elif setting.active_account_id == account.id and not is_active:
        setting.active_account_id = None
        setting.enabled = False
    audit.record(
        db,
        shop_id=shop.id,
        step=audit.Step.CARRIER_CONFIG_CHANGED,
        message=f"{code} {environment.value} credentials saved",
        data={"carrier": code, "environment": environment.value, "account_id": account.id},
        actor=actor,
    )
    return account


def active_account(db: Session, shop: Shop, code: str) -> CarrierAccount | None:
    setting = get_setting(db, shop, code)
    if setting.active_account_id is None:
        return None
    account = db.get(CarrierAccount, setting.active_account_id)
    return account if account and account.is_active else None


def build_adapter(db: Session, shop: Shop, code: str) -> CarrierAdapter:
    account = active_account(db, shop, code)
    if account is None:
        raise NotFoundError(f"No active account for carrier {code}")
    return build_adapter_for_account(db, account)


def build_adapter_for_account(db: Session, account: CarrierAccount) -> CarrierAdapter:
    creds: dict[str, Any] | None = (
        get_encryptor().decrypt_json(account.credentials_enc) if account.credentials_enc else None
    )
    if not creds:
        creds = get_settings().carrier_env_credentials(account.carrier_code) or {}
    return registry.build_adapter(
        account.carrier_code,
        account.environment,
        creds,
        account_id=account.id,
        allow_mock=get_settings().mock_carriers_enabled(),
    )


def adapter_for_shipment(db: Session, shipment: Shipment) -> CarrierAdapter:
    """The adapter of the account that CREATED the shipment (not necessarily today's active
    account), so a mock shipment is never cancelled or tracked through a production adapter."""
    account = (
        db.get(CarrierAccount, shipment.carrier_account_id) if shipment.carrier_account_id else None
    )
    if account is None:
        raise NotFoundError(f"Carrier account for shipment {shipment.id} no longer exists")
    return build_adapter_for_account(db, account)


def account_by_webhook_token(db: Session, carrier_code: str, token: str) -> CarrierAccount | None:
    account = db.scalar(select(CarrierAccount).where(CarrierAccount.webhook_token == token))
    if account is None or account.carrier_code != carrier_code or not account.is_active:
        return None
    return account


def warehouse_ref(db: Session, account_id: int | None, warehouse_id: int) -> str | None:
    if account_id is None:
        return None
    return db.scalar(
        select(CarrierWarehouseMapping.carrier_warehouse_ref).where(
            CarrierWarehouseMapping.carrier_account_id == account_id,
            CarrierWarehouseMapping.warehouse_id == warehouse_id,
        )
    )


# --- warehouse mappings ----------------------------------------------------------------------


def mappings_for_account(db: Session, account_id: int) -> list[CarrierWarehouseMapping]:
    return list(
        db.scalars(
            select(CarrierWarehouseMapping)
            .where(CarrierWarehouseMapping.carrier_account_id == account_id)
            .order_by(CarrierWarehouseMapping.warehouse_id)
        )
    )


def set_mappings(
    db: Session, shop: Shop, code: str, mappings: list[dict[str, Any]], *, actor: str
) -> list[CarrierWarehouseMapping]:
    account = active_account(db, shop, code)
    if account is None:
        raise ValidationFailed("Save credentials for this carrier before mapping warehouses")
    shop_warehouses = {
        w.id for w in db.scalars(select(Warehouse).where(Warehouse.shop_id == shop.id))
    }
    existing = {m.warehouse_id: m for m in mappings_for_account(db, account.id)}
    for item in mappings:
        wid = int(item["warehouse_id"])
        if wid not in shop_warehouses:
            raise ValidationFailed(f"Unknown warehouse {wid}")
        mapping = existing.get(wid)
        if mapping is None:
            mapping = CarrierWarehouseMapping(carrier_account_id=account.id, warehouse_id=wid)
            db.add(mapping)
            existing[wid] = mapping
        mapping.carrier_warehouse_ref = item.get("carrier_warehouse_ref") or None
        mapping.enabled = bool(item.get("enabled", True))
    db.flush()
    audit.record(
        db,
        shop_id=shop.id,
        step=audit.Step.CARRIER_CONFIG_CHANGED,
        message=f"{code} warehouse mappings updated",
        data={"carrier": code, "mappings": mappings},
        actor=actor,
    )
    return mappings_for_account(db, account.id)


# --- engine view -----------------------------------------------------------------------------


def load_profiles(db: Session, shop: Shop) -> dict[str, CarrierProfile]:
    """Every registered carrier as the allocation engine sees it."""
    profiles: dict[str, CarrierProfile] = {}
    for code in registry.registered_codes():
        setting = get_setting(db, shop, code)
        account = active_account(db, shop, code)
        warehouses = (
            frozenset(m.warehouse_id for m in mappings_for_account(db, account.id) if m.enabled)
            if account
            else frozenset()
        )
        profiles[code] = CarrierProfile(
            carrier_code=code,
            enabled=setting.enabled and account is not None,
            cod_enabled=setting.cod_enabled,
            prepaid_enabled=setting.prepaid_enabled,
            priority=setting.priority,
            max_shipping_cost=setting.max_shipping_cost,
            min_weight_g=setting.min_weight_g,
            max_weight_g=setting.max_weight_g,
            performance_score=setting.performance_score,
            supported_warehouse_ids=warehouses,
        )
    return profiles
