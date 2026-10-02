"""Retry policy: exponential backoff with jitter for transient failures only."""

from __future__ import annotations

import random

BASE_SECONDS = 30
CAP_SECONDS = 3600
MAX_RETRIES = 8


def backoff_seconds(retries: int, *, base: int = BASE_SECONDS, cap: int = CAP_SECONDS) -> int:
    """30s, 60s, 120s, ... capped at 1h, with +/-20% jitter so retries do not synchronise."""
    delay = min(cap, base * (2 ** max(retries, 0)))
    return max(1, int(delay * random.uniform(0.8, 1.2)))  # noqa: S311 - jitter, not crypto
