"""Domain enums shared by models, services, carriers and the API.

Values are stored as strings in PostgreSQL (non-native enums) so adding a value never needs an
`ALTER TYPE` migration.
"""

from __future__ import annotations

from enum import StrEnum


class PaymentMode(StrEnum):
    COD = "COD"
    PREPAID = "PREPAID"
    UNKNOWN = "UNKNOWN"


class CarrierEnvironment(StrEnum):
    MOCK = "MOCK"
    SANDBOX = "SANDBOX"
    PRODUCTION = "PRODUCTION"


class LogisticsStatus(StrEnum):
    """Internal processing state of a Shopify fulfillment order."""

    RECEIVED = "RECEIVED"
    SETTLING = "SETTLING"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    AWAITING_ALLOCATION = "AWAITING_ALLOCATION"
    ALLOCATING = "ALLOCATING"
    ALLOCATED = "ALLOCATED"
    NO_CARRIER_AVAILABLE = "NO_CARRIER_AVAILABLE"
    SHIPMENT_PENDING = "SHIPMENT_PENDING"
    RECONCILING = "RECONCILING"
    SHIPPED = "SHIPPED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    AUTOMATION_DISABLED = "AUTOMATION_DISABLED"
    CANCELLED = "CANCELLED"


class TrackingStatus(StrEnum):
    """Normalised carrier tracking statuses. Every adapter maps its raw codes onto these."""

    AWB_CREATED = "AWB_CREATED"
    PICKUP_SCHEDULED = "PICKUP_SCHEDULED"
    PICKED_UP = "PICKED_UP"
    IN_TRANSIT = "IN_TRANSIT"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    DELIVERY_FAILED = "DELIVERY_FAILED"
    RTO_INITIATED = "RTO_INITIATED"
    RTO_IN_TRANSIT = "RTO_IN_TRANSIT"
    RTO_DELIVERED = "RTO_DELIVERED"
    CANCELLED = "CANCELLED"
    EXCEPTION = "EXCEPTION"


class ShipmentStatus(StrEnum):
    """Lifecycle of a shipment: creation states plus every normalised tracking status."""

    CREATING = "CREATING"
    CREATION_UNKNOWN = "CREATION_UNKNOWN"
    CREATE_FAILED = "CREATE_FAILED"
    AWB_CREATED = "AWB_CREATED"
    PICKUP_SCHEDULED = "PICKUP_SCHEDULED"
    PICKED_UP = "PICKED_UP"
    IN_TRANSIT = "IN_TRANSIT"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    DELIVERY_FAILED = "DELIVERY_FAILED"
    RTO_INITIATED = "RTO_INITIATED"
    RTO_IN_TRANSIT = "RTO_IN_TRANSIT"
    RTO_DELIVERED = "RTO_DELIVERED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    EXCEPTION = "EXCEPTION"


#: Shipment statuses that do NOT occupy the one-active-shipment-per-fulfillment-order slot.
INACTIVE_SHIPMENT_STATUSES: frozenset[ShipmentStatus] = frozenset(
    {ShipmentStatus.CANCELLED, ShipmentStatus.CREATE_FAILED}
)


class ShopifySyncStatus(StrEnum):
    PENDING = "PENDING"
    SYNCED = "SYNCED"
    FAILED = "FAILED"


class AllocationStrategy(StrEnum):
    FASTEST = "FASTEST"
    CHEAPEST = "CHEAPEST"
    BALANCED = "BALANCED"
    PRIORITY = "PRIORITY"
    CUSTOM = "CUSTOM"


class WebhookStatus(StrEnum):
    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"
    IGNORED = "IGNORED"


class LogLevel(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class TrackingSource(StrEnum):
    WEBHOOK = "WEBHOOK"
    POLL = "POLL"
    MANUAL = "MANUAL"
    SYSTEM = "SYSTEM"


class AlertSeverity(StrEnum):
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {AlertSeverity.WARNING: 0, AlertSeverity.ERROR: 1, AlertSeverity.CRITICAL: 2}


class AlertStatus(StrEnum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    """Every delivery attempt failed (see `last_error`)."""
    SKIPPED = "SKIPPED"
    """No channel configured, or below ALERT_MIN_SEVERITY. Kept for the record only."""
