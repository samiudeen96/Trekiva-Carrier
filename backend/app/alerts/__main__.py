"""Alert commands for operators.

    python -m app.alerts test       send a test message to every configured channel
    python -m app.alerts dispatch   deliver queued alerts now (normally done by Celery beat)
"""

from __future__ import annotations

import sys

from app.alerts.channels import configured_channels
from app.alerts.dispatch import dispatch_pending, send_test
from app.core.config import get_settings


def main(argv: list[str]) -> int:
    command = argv[0] if argv else ""
    if command == "test":
        if not configured_channels(get_settings()):
            print("No alert channel configured (set ALERT_SLACK_WEBHOOK_URL and/or SMTP_* + ALERT_EMAIL_TO).")
            return 1
        results = send_test()
        for name, outcome in results.items():
            print(f"{name}: {outcome}")
        return 0 if all(v == "ok" for v in results.values()) else 1
    if command == "dispatch":
        print(dispatch_pending())
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
