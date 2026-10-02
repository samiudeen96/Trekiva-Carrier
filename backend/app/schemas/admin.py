"""Request/response models for the embedded admin API."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import AllocationStrategy, CarrierEnvironment
from app.schemas.settings import ShopSettings


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class SessionOut(BaseModel):
    shop_domain: str
    user_id: str | None
    automation_enabled: bool
    app_env: str
    mock_carriers_enabled: bool
    api_version: str


class SettingsOut(BaseModel):
    automation_enabled: bool
    settings: ShopSettings


class SettingsIn(BaseModel):
    automation_enabled: bool | None = None
    settings: ShopSettings | None = None


# --- carriers -------------------------------------------------------------------------------


class CarrierSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    cod_enabled: bool | None = None
    prepaid_enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=10_000)
    max_shipping_cost: Decimal | None = Field(default=None, ge=0)
    min_weight_g: int | None = Field(default=None, ge=0)
    max_weight_g: int | None = Field(default=None, ge=0)
    performance_score: Decimal | None = Field(default=None, ge=0, le=100)


class CarrierAccountIn(BaseModel):
    environment: CarrierEnvironment
    label: str = Field(default="default", min_length=1, max_length=100)
    credentials: dict[str, Any] = Field(default_factory=dict)
    """Write-only. Blank secret fields keep the stored value."""
    is_active: bool = True
    make_active: bool = True


class CarrierAccountOut(_Out):
    id: int
    environment: CarrierEnvironment
    label: str
    credentials_hint: dict[str, Any]
    is_active: bool
    webhook_path: str
    updated_at: datetime


class WarehouseMappingIn(BaseModel):
    warehouse_id: int
    carrier_warehouse_ref: str | None = None
    enabled: bool = True


class WarehouseMappingOut(_Out):
    warehouse_id: int
    carrier_warehouse_ref: str | None
    enabled: bool


class CarrierSettingsOut(_Out):
    enabled: bool
    cod_enabled: bool
    prepaid_enabled: bool
    priority: int
    max_shipping_cost: Decimal | None
    min_weight_g: int | None
    max_weight_g: int | None
    performance_score: Decimal | None
    active_account_id: int | None


class CarrierOut(BaseModel):
    code: str
    display_name: str
    has_real_adapter: bool
    has_mock_adapter: bool
    implementation_status: str
    capabilities: dict[str, bool]
    credentials_schema: dict[str, Any]
    mock_credentials_schema: dict[str, Any] | None
    settings: CarrierSettingsOut
    accounts: list[CarrierAccountOut]
    warehouse_mappings: list[WarehouseMappingOut]


# --- warehouses -----------------------------------------------------------------------------


class PackageDefaults(BaseModel):
    length_cm: Decimal | None = Field(default=None, gt=0)
    breadth_cm: Decimal | None = Field(default=None, gt=0)
    height_cm: Decimal | None = Field(default=None, gt=0)
    min_weight_g: int | None = Field(default=None, gt=0)


class WarehouseIn(BaseModel):
    code: str = Field(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=255)
    contact_name: str | None = None
    phone: str = Field(min_length=5, max_length=32)
    email: str | None = None
    address1: str = Field(min_length=1)
    address2: str | None = None
    city: str = Field(min_length=1)
    state: str = Field(min_length=1)
    pincode: str = Field(pattern=r"^\d{6}$")
    country: str = Field(default="IN", min_length=2, max_length=2)
    shopify_location_id: str | None = Field(default=None, pattern=r"^gid://shopify/Location/\d+$")
    default_package: PackageDefaults = PackageDefaults()
    is_active: bool = True


class WarehouseOut(_Out):
    id: int
    code: str
    name: str
    contact_name: str | None
    phone: str
    email: str | None
    address1: str
    address2: str | None
    city: str
    state: str
    pincode: str
    country: str
    shopify_location_id: str | None
    default_package: dict[str, Any]
    is_active: bool


# --- allocation rules -----------------------------------------------------------------------


class AllocationRuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    priority: int = Field(default=100, ge=0, le=1_000_000)
    is_active: bool = True
    strategy: AllocationStrategy = AllocationStrategy.FASTEST
    conditions: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)


class AllocationRuleOut(_Out):
    id: int
    name: str
    priority: int
    is_active: bool
    strategy: AllocationStrategy
    conditions: dict[str, Any]
    params: dict[str, Any]
    updated_at: datetime
