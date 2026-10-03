"""Alert delivery channels: Slack (incoming webhook) and email (SMTP).

A channel is enabled by configuring it (see `.env.example`). `send` raises on failure; the
dispatcher records the error and retries that channel only.
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

import httpx

from app.core.config import Settings
from app.core.enums import AlertSeverity

TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class AlertMessage:
    severity: AlertSeverity
    subject: str
    body: str


class Channel(Protocol):
    name: str

    def send(self, message: AlertMessage) -> None: ...


_SLACK_ICON = {
    AlertSeverity.WARNING: ":warning:",
    AlertSeverity.ERROR: ":x:",
    AlertSeverity.CRITICAL: ":rotating_light:",
}


def _slack_escape(text: str) -> str:
    """Carrier and Shopify error text is untrusted: `<!channel>` or `<url|label>` in it must
    show as text, not act as a mention or a disguised link."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SlackChannel:
    name = "slack"

    def __init__(self, webhook_url: str) -> None:
        self._url = webhook_url

    def payload(self, message: AlertMessage) -> dict[str, str]:
        subject, body = _slack_escape(message.subject), _slack_escape(message.body)
        return {"text": f"{_SLACK_ICON[message.severity]} *{subject}*\n{body}"}

    def send(self, message: AlertMessage) -> None:
        response = httpx.post(self._url, json=self.payload(message), timeout=TIMEOUT_SECONDS)
        if response.status_code >= 300:
            # The URL is a secret: never include it in the error.
            raise RuntimeError(f"Slack returned HTTP {response.status_code}: {response.text[:200]}")


class EmailChannel:
    name = "email"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        security: str,
        username: str,
        password: str,
        sender: str,
        recipients: list[str],
    ) -> None:
        self._host = host
        self._port = port
        self._security = security
        self._username = username
        self._password = password
        self._sender = sender
        self._recipients = recipients

    def build(self, message: AlertMessage) -> EmailMessage:
        email = EmailMessage()
        email["Subject"] = f"[Trekiva {message.severity.value}] {message.subject}"
        email["From"] = self._sender
        email["To"] = ", ".join(self._recipients)
        email.set_content(message.body)
        return email

    def send(self, message: AlertMessage) -> None:
        email = self.build(message)
        context = ssl.create_default_context()
        smtp: smtplib.SMTP
        if self._security == "ssl":
            smtp = smtplib.SMTP_SSL(
                self._host, self._port, timeout=TIMEOUT_SECONDS, context=context
            )
        else:
            smtp = smtplib.SMTP(self._host, self._port, timeout=TIMEOUT_SECONDS)
        with smtp:
            if self._security == "starttls":
                smtp.starttls(context=context)
            if self._username:
                smtp.login(self._username, self._password)
            smtp.send_message(email)


def configured_channels(settings: Settings) -> list[Channel]:
    channels: list[Channel] = []
    slack_url = settings.alert_slack_webhook_url.get_secret_value()
    if slack_url:
        channels.append(SlackChannel(slack_url))
    recipients = settings.alert_email_recipients
    if recipients and settings.smtp_host:
        channels.append(
            EmailChannel(
                host=settings.smtp_host,
                port=settings.smtp_port,
                security=settings.smtp_security,
                username=settings.smtp_username,
                password=settings.smtp_password.get_secret_value(),
                sender=settings.alert_email_from or settings.smtp_username or recipients[0],
                recipients=recipients,
            )
        )
    return channels
