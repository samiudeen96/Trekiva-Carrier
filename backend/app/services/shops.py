"""Shop installation, encrypted token storage and authenticated Shopify clients."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.crypto import get_encryptor
from app.core.errors import AuthorizationError
from app.core.http import get_http_client
from app.core.time import utcnow
from app.models import Shop
from app.schemas.settings import ShopSettings
from app.services import audit
from app.shopify.api import ShopifyAdmin
from app.shopify.auth import OfflineToken, exchange_session_token, refresh_offline_token
from app.shopify.client import ShopifyGraphQLClient
from app.shopify.errors import ShopifyAuthError

REFRESH_MARGIN = timedelta(minutes=5)


def get_by_domain(db: Session, shop_domain: str) -> Shop | None:
    return db.scalar(select(Shop).where(Shop.shop_domain == shop_domain.lower()))


def get_or_create(db: Session, shop_domain: str) -> Shop:
    shop = get_by_domain(db, shop_domain)
    if shop is None:
        shop = Shop(
            shop_domain=shop_domain.lower(), settings=ShopSettings().model_dump(mode="json")
        )
        db.add(shop)
        db.flush()
    return shop


def store_token(db: Session, shop: Shop, token: OfflineToken) -> None:
    enc = get_encryptor()
    shop.access_token_enc = enc.encrypt(token.access_token)
    shop.access_token_expires_at = token.expires_at
    if token.refresh_token:
        shop.refresh_token_enc = enc.encrypt(token.refresh_token)
        shop.refresh_token_expires_at = token.refresh_token_expires_at
    shop.scopes = token.scope or shop.scopes
    db.flush()


def install_from_session_token(db: Session, shop_domain: str, session_token: str) -> Shop:
    """Exchange an App Bridge session token for an offline access token and store it."""
    settings = get_settings()
    if not settings.is_shop_allowed(shop_domain):
        raise AuthorizationError(f"{shop_domain} is not allowed to use this app")
    token = exchange_session_token(
        get_http_client(),
        shop_domain=shop_domain,
        session_token=session_token,
        client_id=settings.shopify_client_id,
        client_secret=settings.shopify_client_secret.get_secret_value(),
        expiring=settings.shopify_expiring_offline_tokens,
    )
    shop = get_or_create(db, shop_domain)
    was_installed = shop.is_installed
    store_token(db, shop, token)
    shop.uninstalled_at = None
    if not was_installed:
        shop.installed_at = utcnow()
        audit.record(
            db,
            shop_id=shop.id,
            step=audit.Step.APP_INSTALLED,
            message=f"App installed on {shop_domain}",
        )
    return shop


def needs_token(shop: Shop | None) -> bool:
    if shop is None or not shop.is_installed:
        return True
    expires = shop.access_token_expires_at
    if expires is None:
        return False
    # Expired and not refreshable -> need a new token exchange.
    return expires <= utcnow() and shop.refresh_token_enc is None


def mark_uninstalled(db: Session, shop: Shop) -> None:
    shop.access_token_enc = None
    shop.refresh_token_enc = None
    shop.access_token_expires_at = None
    shop.refresh_token_expires_at = None
    shop.uninstalled_at = utcnow()
    audit.record(
        db,
        shop_id=shop.id,
        step=audit.Step.APP_UNINSTALLED,
        message="App uninstalled; tokens revoked",
    )


def access_token(db: Session, shop: Shop) -> str:
    """A valid offline access token, refreshing an expiring one when needed."""
    if not shop.is_installed or shop.access_token_enc is None:
        raise ShopifyAuthError(f"{shop.shop_domain} has no access token (app not installed)")
    enc = get_encryptor()
    expires = shop.access_token_expires_at
    if expires is not None and expires - REFRESH_MARGIN <= utcnow():
        if shop.refresh_token_enc is None:
            raise ShopifyAuthError(
                f"{shop.shop_domain} access token expired; reopen the app in Shopify admin"
            )
        settings = get_settings()
        token = refresh_offline_token(
            get_http_client(),
            shop_domain=shop.shop_domain,
            refresh_token=enc.decrypt(shop.refresh_token_enc),
            client_id=settings.shopify_client_id,
            client_secret=settings.shopify_client_secret.get_secret_value(),
        )
        store_token(db, shop, token)
        return token.access_token
    return enc.decrypt(shop.access_token_enc)


def shopify_admin(db: Session, shop: Shop) -> ShopifyAdmin:
    settings = get_settings()
    client = ShopifyGraphQLClient(
        shop_domain=shop.shop_domain,
        access_token=access_token(db, shop),
        api_version=settings.shopify_api_version,
        http=get_http_client(),
    )
    return ShopifyAdmin(client)


def load_settings(shop: Shop) -> ShopSettings:
    return ShopSettings.load(shop.settings)


AdminFactory = Callable[[Session, Shop], ShopifyAdmin]


def default_admin_factory(db: Session, shop: Shop) -> ShopifyAdmin:
    return shopify_admin(db, shop)
