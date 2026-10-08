"""IP address -> approximate location, from a local MaxMind City database (no network calls, so
customer IPs never leave the server). Configured with GEOIP_CITY_DB_PATH."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.core.config import get_settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IpLocation:
    country_code: str | None
    state_code: str | None
    state_name: str | None
    city: str | None

    def describe(self) -> str:
        return (
            ", ".join(p for p in (self.city, self.state_name, self.country_code) if p) or "unknown"
        )


@lru_cache(maxsize=1)
def _reader() -> Any:
    path = get_settings().geoip_city_db_path
    if not path:
        return None
    try:
        import geoip2.database

        return geoip2.database.Reader(path)
    except Exception:
        log.exception(
            "Could not open GeoIP database; IP location checks are off", extra={"path": path}
        )
        return None


def is_configured() -> bool:
    return _reader() is not None


def lookup(ip: str) -> IpLocation | None:
    """Location of `ip`, or None when unknown (no database, private or unlisted address)."""
    reader = _reader()
    if reader is None:
        return None
    try:
        r = reader.city(ip)
    except Exception:  # AddressNotFoundError, or a malformed address
        return None
    subdivision = r.subdivisions.most_specific
    return IpLocation(
        country_code=r.country.iso_code,
        state_code=subdivision.iso_code,
        state_name=subdivision.name,
        city=r.city.name,
    )
