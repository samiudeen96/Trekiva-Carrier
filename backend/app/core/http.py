"""Process-wide HTTP client for Shopify calls (httpx.Client is thread-safe and pools connections)."""

from __future__ import annotations

from functools import lru_cache

import httpx

from app.core.config import get_settings


@lru_cache
def get_http_client() -> httpx.Client:
    settings = get_settings()
    timeout = settings.shopify_http_timeout_seconds
    return httpx.Client(
        timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
        headers={"User-Agent": "TrekivaLogistics/0.1"},
    )
