"""Application-level exceptions (carrier and Shopify errors live in their own packages)."""

from __future__ import annotations


class TrekivaError(Exception):
    """Base class for application errors."""

    status_code: int = 400
    code: str = "error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class ConfigurationError(TrekivaError):
    status_code = 500
    code = "configuration_error"


class NotFoundError(TrekivaError):
    status_code = 404
    code = "not_found"


class AuthenticationError(TrekivaError):
    status_code = 401
    code = "unauthenticated"


class AuthorizationError(TrekivaError):
    status_code = 403
    code = "forbidden"


class ValidationFailed(TrekivaError):
    status_code = 422
    code = "validation_failed"


class ConflictError(TrekivaError):
    status_code = 409
    code = "conflict"


class InvalidTransition(TrekivaError):
    status_code = 409
    code = "invalid_transition"
