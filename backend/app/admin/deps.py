"""Authentication for the embedded admin API.

Every request must carry `Authorization: Bearer <App Bridge session token>`. App Bridge adds it
automatically to `fetch` calls made from the embedded app. The token is verified (signature,
expiry, audience, shop) and, if we hold no valid offline token yet, exchanged for one.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import AuthenticationError, AuthorizationError
from app.models import Shop
from app.services import shops
from app.shopify.security import verify_session_token


@dataclass(frozen=True)
class AdminContext:
    shop: Shop
    user_id: str | None

    @property
    def actor(self) -> str:
        return f"staff:{self.user_id}" if self.user_id else "staff"


def admin_context(request: Request, db: Session = Depends(get_db)) -> AdminContext:
    settings = get_settings()

    bypass = settings.dev_auth_bypass_shop
    if bypass and settings.app_env == "development":
        shop = shops.get_or_create(db, bypass)
        return AdminContext(shop=shop, user_id="dev")

    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise AuthenticationError("Missing session token")
    claims = verify_session_token(
        header[7:].strip(),
        client_id=settings.shopify_client_id,
        client_secret=settings.shopify_client_secret.get_secret_value(),
    )
    if not settings.is_shop_allowed(claims.shop_domain):
        raise AuthorizationError("This shop is not allowed to use Trekiva Logistics")

    existing = shops.get_by_domain(db, claims.shop_domain)
    if existing is None or shops.needs_token(existing):
        existing = shops.install_from_session_token(db, claims.shop_domain, claims.raw_token)
        db.commit()
    return AdminContext(shop=existing, user_id=claims.user_id)
