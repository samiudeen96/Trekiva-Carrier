from __future__ import annotations

from typing import Any


class ShopifyError(Exception):
    retryable: bool = False

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.details = details


class ShopifyTransientError(ShopifyError):
    """Network failure, 5xx, or throttling that outlasted the client's own retries."""

    retryable = True


class ShopifyAuthError(ShopifyError):
    """Invalid/expired/revoked access token or missing scope."""


class ShopifyGraphQLError(ShopifyError):
    """Top-level GraphQL `errors` (bad query, missing field for this API version, ...)."""


class ShopifyUserError(ShopifyError):
    """A mutation returned `userErrors`."""

    def __init__(self, message: str, *, user_errors: list[dict[str, Any]]) -> None:
        super().__init__(message, details=user_errors)
        self.user_errors = user_errors
