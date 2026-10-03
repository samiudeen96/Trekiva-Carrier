"""ORM models. Importing this package registers every table on `Base.metadata`."""

from app.models.alert import Alert
from app.models.allocation import AllocationRule
from app.models.audit import AutomationLog
from app.models.carrier import CarrierAccount, CarrierSetting, CarrierWarehouseMapping
from app.models.order import ShopifyFulfillmentOrder, ShopifyOrder
from app.models.quote import CarrierQuote, CarrierServiceabilityCheck
from app.models.shipment import Shipment
from app.models.shop import Shop
from app.models.tracking import TrackingEvent
from app.models.warehouse import Warehouse
from app.models.webhook import WebhookEvent

__all__ = [
    "Alert",
    "AllocationRule",
    "AutomationLog",
    "CarrierAccount",
    "CarrierQuote",
    "CarrierServiceabilityCheck",
    "CarrierSetting",
    "CarrierWarehouseMapping",
    "Shipment",
    "Shop",
    "ShopifyFulfillmentOrder",
    "ShopifyOrder",
    "TrackingEvent",
    "Warehouse",
    "WebhookEvent",
]
