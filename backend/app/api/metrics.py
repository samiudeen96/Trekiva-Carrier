from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.ops.metrics import collect, render

router = APIRouter(tags=["health"])


@router.get("/metrics", include_in_schema=False)
def metrics(
    authorization: str | None = Header(default=None), db: Session = Depends(get_db)
) -> Response:
    """Prometheus scrape endpoint. Disabled (404) unless METRICS_TOKEN is set; requires
    `Authorization: Bearer <METRICS_TOKEN>`."""
    token = get_settings().metrics_token.get_secret_value()
    if not token:
        return Response(status_code=404)
    if not hmac.compare_digest((authorization or "").encode(), f"Bearer {token}".encode()):
        return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    return PlainTextResponse(render(collect(db)), media_type="text/plain; version=0.0.4")
