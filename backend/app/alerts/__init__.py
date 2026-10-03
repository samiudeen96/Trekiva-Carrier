"""Operator alerts (email / Slack) for failures that need a person.

Importing this package registers the state-change listener (`triggers.py`). Raise alerts with
`raise_alert` / `carrier_error`; `dispatch.py` delivers them.
"""

from app.alerts import triggers as _triggers  # noqa: F401  (registers the flush listener)
from app.alerts.service import (
    AlertKind,
    carrier_error,
    has_open_alert,
    raise_alert,
)

__all__ = ["AlertKind", "carrier_error", "has_open_alert", "raise_alert"]
