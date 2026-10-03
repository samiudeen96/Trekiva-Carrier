"""Error monitoring (Sentry). Off unless SENTRY_DSN is set.

The app handles Shopify *protected customer data* (names, phones, addresses), so Sentry is
configured to never receive it: no default PII, no request bodies (webhook payloads are customer
data), no local variables in stack traces.
"""

from __future__ import annotations

import logging

from app.core.config import get_settings

log = logging.getLogger(__name__)


def init_sentry(component: str) -> bool:
    """Initialise Sentry for this process. `component` is api, worker or beat."""
    settings = get_settings()
    dsn = settings.sentry_dsn.get_secret_value()
    if not dsn:
        return False
    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=settings.sentry_environment or settings.app_env,
        release=settings.sentry_release or None,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,
        max_request_body_size="never",
        include_local_variables=False,
    )
    sentry_sdk.set_tag("component", component)
    log.info("Sentry enabled", extra={"component": component})
    return True
