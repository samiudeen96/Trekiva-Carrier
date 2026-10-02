"""Offline access tokens via Shopify token exchange (no OAuth redirect dance).

Flow: the embedded admin sends an App Bridge session token -> we exchange it for an *offline*
access token (used by background workers) and store it encrypted. When Shopify issues expiring
offline tokens, we also store the refresh token and refresh ahead of expiry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.core.time import utcnow
from app.shopify.errors import ShopifyAuthError, ShopifyTransientError

TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"  # noqa: S105
ID_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:id_token"  # noqa: S105
OFFLINE_TOKEN_TYPE = "urn:shopify:params:oauth:token-type:offline-access-token"  # noqa: S105


@dataclass(frozen=True)
class OfflineToken:
    access_token: str
    scope: str | None
    expires_at: datetime | None
    refresh_token: str | None
    refresh_token_expires_at: datetime | None


def _parse_token_response(body: dict[str, Any]) -> OfflineToken:
    now = utcnow()
    expires_in = body.get("expires_in")
    refresh_expires_in = body.get("refresh_token_expires_in")
    return OfflineToken(
        access_token=str(body["access_token"]),
        scope=body.get("scope"),
        expires_at=now + timedelta(seconds=int(expires_in)) if expires_in else None,
        refresh_token=body.get("refresh_token"),
        refresh_token_expires_at=(
            now + timedelta(seconds=int(refresh_expires_in)) if refresh_expires_in else None
        ),
    )


def _post_token(http: httpx.Client, shop_domain: str, payload: dict[str, Any]) -> OfflineToken:
    url = f"https://{shop_domain}/admin/oauth/access_token"
    try:
        resp = http.post(url, json=payload, headers={"Accept": "application/json"})
    except httpx.TransportError as exc:
        raise ShopifyTransientError(f"Token request failed: {exc}") from exc
    if resp.status_code >= 500 or resp.status_code == 429:
        raise ShopifyTransientError(f"Token endpoint returned {resp.status_code}")
    if resp.status_code != 200:
        raise ShopifyAuthError(f"Token endpoint returned {resp.status_code}: {resp.text[:300]}")
    return _parse_token_response(resp.json())


def exchange_session_token(
    http: httpx.Client,
    *,
    shop_domain: str,
    session_token: str,
    client_id: str,
    client_secret: str,
    expiring: bool,
) -> OfflineToken:
    payload: dict[str, Any] = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": TOKEN_EXCHANGE_GRANT,
        "subject_token": session_token,
        "subject_token_type": ID_TOKEN_TYPE,
        "requested_token_type": OFFLINE_TOKEN_TYPE,
    }
    if expiring:
        payload["expiring"] = "1"
    return _post_token(http, shop_domain, payload)


def refresh_offline_token(
    http: httpx.Client,
    *,
    shop_domain: str,
    refresh_token: str,
    client_id: str,
    client_secret: str,
) -> OfflineToken:
    return _post_token(
        http,
        shop_domain,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )
