"""Shopify webhook ingress: verify HMAC -> store idempotently -> enqueue -> 200 fast."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.enums import WebhookStatus
from app.shopify.security import is_valid_shop_domain, verify_webhook_hmac
from app.webhooks.ingest import store_event
from app.workers.enqueue import enqueue_webhook_event

log = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

SOURCE = "shopify"


def _ingest(raw: bytes, headers: dict[str, str]) -> tuple[int, bool, WebhookStatus]:
    settings = get_settings()
    topic = headers.get("x-shopify-topic", "unknown")
    shop_domain = (headers.get("x-shopify-shop-domain") or "").lower() or None
    event_id = headers.get("x-shopify-event-id") or headers.get("x-shopify-webhook-id")
    try:
        payload = json.loads(raw) if raw else {}
    except ValueError:
        payload = {"_unparseable": raw[:1000].decode(errors="replace")}
    if not isinstance(payload, dict):
        payload = {"_payload": payload}

    status, note = WebhookStatus.RECEIVED, None
    if (
        not shop_domain
        or not is_valid_shop_domain(shop_domain)
        or not settings.is_shop_allowed(shop_domain)
    ):
        status, note = WebhookStatus.IGNORED, "Shop not allowed"
    with session_scope() as db:
        event, created = store_event(
            db,
            source=SOURCE,
            topic=topic,
            external_event_id=event_id,
            shop_domain=shop_domain,
            raw_body=raw,
            payload=payload,
            headers=headers,
            status=status,
            error_message=note,
        )
        return event.id, created, event.processing_status


@router.post("/webhooks/shopify")
async def shopify_webhook(request: Request) -> Response:
    raw = await request.body()
    secret = get_settings().shopify_client_secret.get_secret_value()
    if not verify_webhook_hmac(raw, request.headers.get("x-shopify-hmac-sha256"), secret):
        log.warning("Rejected Shopify webhook with invalid HMAC")
        return JSONResponse({"error": "invalid hmac"}, status_code=401)

    headers = {k.lower(): v for k, v in request.headers.items()}
    event_id, created, status = await run_in_threadpool(_ingest, raw, headers)
    if created and status == WebhookStatus.RECEIVED:
        enqueue_webhook_event(event_id)
    return JSONResponse({"status": "accepted" if created else "duplicate", "id": event_id})
