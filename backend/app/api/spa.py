"""Serve the embedded admin UI (React build) with the headers Shopify requires.

`frame-ancestors` restricts framing to the shop's admin (clickjacking protection required for
embedded apps). The Shopify API key is injected at serve time so one image works everywhere.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings
from app.shopify.security import is_valid_shop_domain

router = APIRouter(include_in_schema=False)

API_KEY_PLACEHOLDER = "__SHOPIFY_API_KEY__"

_FALLBACK_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Trekiva Logistics</title></head>
<body><p>Admin UI not built. Run <code>npm run build</code> in admin-ui/.</p></body></html>"""


def _dist() -> Path:
    path = Path(get_settings().admin_ui_dist)
    if not path.is_absolute():
        path = (Path(__file__).resolve().parents[2] / path).resolve()
    return path


@lru_cache
def _index_template() -> str:
    index = _dist() / "index.html"
    return index.read_text() if index.exists() else _FALLBACK_HTML


def _frame_ancestors(shop: str | None) -> str:
    settings = get_settings()
    shops = [shop] if shop and is_valid_shop_domain(shop) and settings.is_shop_allowed(shop) else []
    if not shops:
        shops = sorted(settings.shop_allowlist)
    sources = " ".join(f"https://{s}" for s in shops) or "https://*.myshopify.com"
    return f"frame-ancestors {sources} https://admin.shopify.com;"


def mount_static(app: FastAPI) -> None:
    assets = _dist() / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")


@router.get("/{path:path}")
def spa(path: str, request: Request) -> Response:
    if path.startswith(("api/", "webhooks/")):
        return Response(status_code=404)
    settings = get_settings()
    html = _index_template().replace(API_KEY_PLACEHOLDER, settings.shopify_client_id)
    return HTMLResponse(
        html,
        headers={
            "Content-Security-Policy": _frame_ancestors(request.query_params.get("shop")),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "strict-origin-when-cross-origin",
        },
    )
