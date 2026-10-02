"""Carrier adapter registry.

Each carrier code may have a real adapter (used for SANDBOX/PRODUCTION accounts) and a mock
adapter (used for MOCK accounts). Both share the same code, so switching an account from MOCK
to PRODUCTION keeps all settings, history and analytics under one carrier.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

from app.carriers.base import CarrierAdapter
from app.carriers.errors import CarrierAuthError
from app.carriers.types import CarrierAccountConfig, CarrierCapabilities
from app.core.enums import CarrierEnvironment
from app.core.errors import ConfigurationError, NotFoundError

A = TypeVar("A", bound=type[CarrierAdapter])

_REAL: dict[str, type[CarrierAdapter]] = {}
_MOCK: dict[str, type[CarrierAdapter]] = {}


def register_carrier(*, mock: bool = False) -> Callable[[A], A]:
    def decorator(cls: A) -> A:
        target = _MOCK if mock else _REAL
        existing = target.get(cls.code)
        if existing is not None and existing is not cls:
            raise ConfigurationError(
                f"Carrier {cls.code!r} registered twice ({'mock' if mock else 'real'})"
            )
        target[cls.code] = cls
        return cls

    return decorator


def registered_codes() -> list[str]:
    return sorted(set(_REAL) | set(_MOCK))


def get_adapter_class(code: str, environment: CarrierEnvironment) -> type[CarrierAdapter]:
    registry = _MOCK if environment == CarrierEnvironment.MOCK else _REAL
    try:
        return registry[code]
    except KeyError:
        raise NotFoundError(
            f"No {'mock' if environment == CarrierEnvironment.MOCK else 'real'} adapter for {code!r}"
        ) from None


class CarrierDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    display_name: str
    has_real_adapter: bool
    has_mock_adapter: bool
    capabilities: CarrierCapabilities
    credentials_json_schema: dict[str, Any]
    mock_credentials_json_schema: dict[str, Any] | None


def describe(code: str) -> CarrierDescriptor:
    real = _REAL.get(code)
    mock = _MOCK.get(code)
    primary = real or mock
    if primary is None:
        raise NotFoundError(f"Unknown carrier {code!r}")
    return CarrierDescriptor(
        code=code,
        display_name=(real.display_name if real else primary.display_name.replace(" (Mock)", "")),
        has_real_adapter=real is not None,
        has_mock_adapter=mock is not None,
        capabilities=primary.capabilities,
        credentials_json_schema=(real or primary).credentials_schema.model_json_schema(),
        mock_credentials_json_schema=mock.credentials_schema.model_json_schema() if mock else None,
    )


def build_adapter(
    code: str,
    environment: CarrierEnvironment,
    credentials: dict[str, Any],
    *,
    account_id: int | None,
    allow_mock: bool,
) -> CarrierAdapter:
    if environment == CarrierEnvironment.MOCK and not allow_mock:
        raise ConfigurationError("Mock carriers are disabled in this environment")
    cls = get_adapter_class(code, environment)
    try:
        creds = cls.credentials_schema.model_validate(credentials)
    except ValidationError as exc:
        raise CarrierAuthError(f"Invalid credentials for {code}: {exc}", carrier_code=code) from exc
    return cls(
        CarrierAccountConfig(
            account_id=account_id, carrier_code=code, environment=environment, credentials=creds
        )
    )
