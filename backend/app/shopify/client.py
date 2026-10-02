"""Minimal, typed GraphQL Admin API client with throttle-aware retries."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from app.shopify.errors import (
    ShopifyAuthError,
    ShopifyGraphQLError,
    ShopifyTransientError,
    ShopifyUserError,
)

log = logging.getLogger(__name__)


class ShopifyGraphQLClient:
    def __init__(
        self,
        *,
        shop_domain: str,
        access_token: str,
        api_version: str,
        http: httpx.Client,
        max_retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.shop_domain = shop_domain
        self.api_version = api_version
        self._access_token = access_token
        self._http = http
        self._max_retries = max_retries
        self._sleep = sleep

    @property
    def endpoint(self) -> str:
        return f"https://{self.shop_domain}/admin/api/{self.api_version}/graphql.json"

    def execute(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run a query/mutation and return `data`. Retries throttling and transient failures.

        Mutations are only retried on failures where Shopify cannot have applied them
        (connection errors, 429/503, THROTTLED); callers must still design mutations to be safe
        to repeat (Shopify rejects fulfilling an already-fulfilled line, for example).
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._http.post(
                    self.endpoint,
                    json={"query": query, "variables": variables or {}},
                    headers={
                        "X-Shopify-Access-Token": self._access_token,
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                if attempt <= self._max_retries:
                    self._backoff(attempt)
                    continue
                raise ShopifyTransientError(f"Shopify unreachable: {exc}") from exc
            except httpx.TransportError as exc:
                raise ShopifyTransientError(f"Shopify request failed: {exc}") from exc

            if resp.status_code in (401, 403):
                raise ShopifyAuthError(f"Shopify rejected credentials ({resp.status_code})")
            if resp.status_code in (429, 503) or resp.status_code >= 500:
                if attempt <= self._max_retries and resp.status_code in (429, 503):
                    self._sleep(_retry_after(resp, default=2.0 * attempt))
                    continue
                raise ShopifyTransientError(f"Shopify returned HTTP {resp.status_code}")
            if resp.status_code != 200:
                raise ShopifyGraphQLError(
                    f"Shopify returned HTTP {resp.status_code}: {resp.text[:500]}"
                )

            body: dict[str, Any] = resp.json()
            errors = body.get("errors")
            if errors:
                if _is_throttled(errors) and attempt <= self._max_retries:
                    self._sleep(_throttle_wait(body, attempt))
                    continue
                if _is_throttled(errors):
                    raise ShopifyTransientError("Shopify GraphQL throttled", details=errors)
                if _is_access_denied(errors):
                    raise ShopifyAuthError("Shopify access denied (missing scope?)", details=errors)
                raise ShopifyGraphQLError(_format_errors(errors), details=errors)
            data = body.get("data")
            if not isinstance(data, dict):
                raise ShopifyGraphQLError("Shopify response missing data")
            return data

    def _backoff(self, attempt: int) -> None:
        self._sleep(min(2.0**attempt, 20.0))


def raise_on_user_errors(payload: dict[str, Any] | None, operation: str) -> dict[str, Any]:
    """Return a mutation payload, raising ShopifyUserError if it carries `userErrors`."""
    if payload is None:
        raise ShopifyGraphQLError(f"{operation}: empty payload")
    user_errors = payload.get("userErrors") or []
    if user_errors:
        messages = "; ".join(str(e.get("message")) for e in user_errors)
        raise ShopifyUserError(f"{operation} failed: {messages}", user_errors=user_errors)
    return payload


def _is_throttled(errors: list[dict[str, Any]]) -> bool:
    return any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors)


def _is_access_denied(errors: list[dict[str, Any]]) -> bool:
    return any((e.get("extensions") or {}).get("code") == "ACCESS_DENIED" for e in errors)


def _format_errors(errors: list[dict[str, Any]]) -> str:
    return "; ".join(str(e.get("message", e)) for e in errors)[:1000]


def _throttle_wait(body: dict[str, Any], attempt: int) -> float:
    """Seconds until enough query cost has been restored (from `extensions.cost`)."""
    cost = (body.get("extensions") or {}).get("cost") or {}
    status = cost.get("throttleStatus") or {}
    try:
        needed = float(cost.get("requestedQueryCost", 0)) - float(status["currentlyAvailable"])
        rate = float(status["restoreRate"])
        if rate > 0:
            return max(0.5, min(needed / rate + 0.25, 30.0))
    except (KeyError, TypeError, ValueError):
        pass
    return min(2.0 * attempt, 10.0)


def _retry_after(resp: httpx.Response, *, default: float) -> float:
    try:
        return max(0.5, min(float(resp.headers["Retry-After"]), 30.0))
    except (KeyError, ValueError):
        return default
