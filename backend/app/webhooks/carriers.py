"""Carrier tracking webhook ingress.

    POST /webhooks/carriers/{carrier_code}/{webhook_token}

The random per-account token identifies the account. The adapter then authenticates the request
its own way (`verify_webhook`) and parses it into normalised `TrackingUpdate`s here, at ingress,
so the stored event already contains carrier-neutral data. A worker applies the updates.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.carriers.errors import CarrierError
from app.core.db import session_scope
from app.core.enums import WebhookStatus
from app.models import Shop
from app.services import carriers
from app.webhooks.ingest import store_event
from app.workers import enqueue

log = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

TOPIC = "tracking"


def _ingest(
    carrier_code: str, token: str, raw: bytes, headers: dict[str, str]
) -> tuple[int, dict[str, Any]]:
    with session_scope() as db:
        account = carriers.account_by_webhook_token(db, carrier_code, token)
        if account is None:
            return 404, {"error": "unknown webhook"}
        adapter = carriers.build_adapter_for_account(db, account)
        if not adapter.verify_webhook(headers, raw):
            log.warning(
                "Rejected carrier webhook with invalid signature", extra={"carrier": carrier_code}
            )
            return 401, {"error": "invalid signature"}
        shop = db.get(Shop, account.shop_id)
        assert shop is not None
        try:
            updates = adapter.process_webhook(headers, raw)
        except (CarrierError, ValueError, KeyError) as exc:
            store_event(
                db,
                source=carrier_code,
                topic=TOPIC,
                external_event_id=None,
                shop_domain=shop.shop_domain,
                raw_body=raw,
                payload={"account_id": account.id, "raw": raw[:5000].decode(errors="replace")},
                headers=headers,
                status=WebhookStatus.FAILED,
                error_message=f"Unparseable carrier webhook: {exc}",
            )
            return 400, {"error": "unparseable payload"}
        event, created = store_event(
            db,
            source=carrier_code,
            topic=TOPIC,
            external_event_id=None,  # payload hash: byte-identical redeliveries are deduplicated
            shop_domain=shop.shop_domain,
            raw_body=raw,
            payload={
                "account_id": account.id,
                "updates": [u.model_dump(mode="json") for u in updates],
                "raw": _json_or_text(raw),
            },
            headers=headers,
        )
        if created:
            enqueue.enqueue_after_commit(db, enqueue.WEBHOOK_PROCESS, event.id)
        return 200, {"status": "accepted" if created else "duplicate", "updates": len(updates)}


def _json_or_text(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw[:5000].decode(errors="replace")


@router.post("/webhooks/carriers/{carrier_code}/{token}")
async def carrier_webhook(carrier_code: str, token: str, request: Request) -> Response:
    raw = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    status, body = await run_in_threadpool(_ingest, carrier_code, token, raw, headers)
    return JSONResponse(body, status_code=status)
