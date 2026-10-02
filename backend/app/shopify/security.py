"""Shopify request authentication: webhook HMAC and App Bridge session tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import jwt

from app.core.errors import AuthenticationError

SHOP_DOMAIN_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]*\.myshopify\.com$")


def is_valid_shop_domain(shop: str | None) -> bool:
    return bool(shop) and SHOP_DOMAIN_RE.fullmatch(shop or "") is not None


def compute_webhook_hmac(raw_body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def verify_webhook_hmac(raw_body: bytes, header_value: str | None, secret: str) -> bool:
    """Verify `X-Shopify-Hmac-Sha256` against the *raw* request body (constant-time)."""
    if not header_value or not secret:
        return False
    return hmac.compare_digest(compute_webhook_hmac(raw_body, secret), header_value.strip())


@dataclass(frozen=True)
class SessionTokenClaims:
    shop_domain: str
    user_id: str | None
    session_id: str | None
    raw_token: str


def verify_session_token(
    token: str, *, client_id: str, client_secret: str, leeway_seconds: int = 10
) -> SessionTokenClaims:
    """Validate an App Bridge session token (HS256 JWT signed with the app's client secret).

    Checks signature, `exp`/`nbf`, audience (= our client id), and that `iss` and `dest` refer to
    the same *.myshopify.com shop.
    """
    if not client_secret:
        raise AuthenticationError("Shopify client secret is not configured")
    try:
        claims = jwt.decode(
            token,
            client_secret,
            algorithms=["HS256"],
            audience=client_id,
            leeway=leeway_seconds,
            options={"require": ["exp", "nbf", "iss", "dest", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthenticationError(f"Invalid session token: {exc}") from exc

    dest_host = urlparse(str(claims["dest"])).hostname or ""
    iss_host = urlparse(str(claims["iss"])).hostname or ""
    if not is_valid_shop_domain(dest_host) or dest_host != iss_host:
        raise AuthenticationError("Session token issuer/destination mismatch")

    sub = claims.get("sub")
    sid = claims.get("sid")
    return SessionTokenClaims(
        shop_domain=dest_host.lower(),
        user_id=str(sub) if sub is not None else None,
        session_id=str(sid) if sid is not None else None,
        raw_token=token,
    )
