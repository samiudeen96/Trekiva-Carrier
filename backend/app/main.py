"""FastAPI application factory."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import carriers as _carriers  # noqa: F401  (registers carrier adapters)
from app.admin.logistics_routes import router as logistics_router
from app.admin.routes import router as admin_router
from app.api.health import router as health_router
from app.api.spa import mount_static
from app.api.spa import router as spa_router
from app.core.config import get_settings
from app.core.errors import TrekivaError
from app.core.logging import configure_logging
from app.shopify.errors import ShopifyAuthError, ShopifyError
from app.webhooks.carriers import router as carrier_webhook_router
from app.webhooks.shopify import router as shopify_webhook_router

log = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.log_json)
    docs = not settings.is_production
    app = FastAPI(
        title="Trekiva Logistics",
        version="0.1.0",
        docs_url="/api/docs" if docs else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if docs else None,
    )

    @app.exception_handler(TrekivaError)
    def _app_error(_: Request, exc: TrekivaError) -> JSONResponse:
        return JSONResponse(
            {"error": exc.code, "message": exc.message}, status_code=exc.status_code
        )

    @app.exception_handler(ShopifyError)
    def _shopify_error(_: Request, exc: ShopifyError) -> JSONResponse:
        status = 401 if isinstance(exc, ShopifyAuthError) else 502
        return JSONResponse({"error": "shopify_error", "message": str(exc)}, status_code=status)

    app.include_router(health_router)
    app.include_router(shopify_webhook_router)
    app.include_router(carrier_webhook_router)
    app.include_router(admin_router)
    app.include_router(logistics_router)
    mount_static(app)
    app.include_router(spa_router)  # catch-all: must be last
    return app


app = create_app()
