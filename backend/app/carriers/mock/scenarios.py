"""Well-known destination pincodes that trigger specific mock carrier behaviour.

Use these in a development store to exercise every branch of the allocation and shipment flow.
Any other pincode is serviceable by both mock carriers.
"""

from __future__ import annotations

from enum import StrEnum


class MockScenario(StrEnum):
    OK = "OK"
    NOT_SERVICEABLE = "NOT_SERVICEABLE"
    COD_UNAVAILABLE = "COD_UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    """Serviceability/quote calls time out (transient)."""
    SERVER_ERROR = "SERVER_ERROR"
    """Serviceability/quote calls return HTTP 500 (transient)."""
    AUTH_FAILURE = "AUTH_FAILURE"
    INVALID_PINCODE = "INVALID_PINCODE"
    CREATE_TIMEOUT = "CREATE_TIMEOUT"
    """Create times out *after* the carrier created the shipment (reconciliation finds it)."""
    CREATE_TIMEOUT_LOST = "CREATE_TIMEOUT_LOST"
    """Create times out and the shipment was NOT created (pair with `lost_references`)."""
    CREATE_SERVER_ERROR = "CREATE_SERVER_ERROR"


EKART = "ekart"
XPRESSBEES = "xpressbees"

DEFAULT_SCENARIOS: dict[str, dict[str, MockScenario]] = {
    # Only XpressBees serviceable
    "999001": {EKART: MockScenario.NOT_SERVICEABLE},
    # Only Ekart serviceable
    "999002": {XPRESSBEES: MockScenario.NOT_SERVICEABLE},
    # Neither serviceable
    "999003": {EKART: MockScenario.NOT_SERVICEABLE, XPRESSBEES: MockScenario.NOT_SERVICEABLE},
    # Serviceable, but COD unavailable on both
    "999004": {EKART: MockScenario.COD_UNAVAILABLE, XPRESSBEES: MockScenario.COD_UNAVAILABLE},
    # Ekart times out / errors on serviceability
    "999010": {EKART: MockScenario.TIMEOUT},
    "999011": {EKART: MockScenario.SERVER_ERROR},
    # XpressBees create times out after creating (ambiguous -> reconcile finds it)
    "999020": {XPRESSBEES: MockScenario.CREATE_TIMEOUT},
    # XpressBees create returns 503
    "999021": {XPRESSBEES: MockScenario.CREATE_SERVER_ERROR},
    # Both reject the pincode at create time
    "999030": {EKART: MockScenario.INVALID_PINCODE, XPRESSBEES: MockScenario.INVALID_PINCODE},
}
