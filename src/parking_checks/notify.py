"""Alert delivery. Every alert goes through Notifier.notify() so channels can change freely.

Rules: a failure on one channel never blocks another; every successful send is recorded in
alerts_sent; a send whose dedupe_key already exists for that channel is skipped.
"""

from __future__ import annotations

import logging
import smtplib
from collections.abc import Callable
from datetime import datetime
from email.message import EmailMessage
from typing import Protocol

import requests

from .config import Config
from .state import State

log = logging.getLogger(__name__)


class Channel(Protocol):
    name: str

    def send(self, subject: str, body: str, short: str) -> None: ...


class EmailChannel:
    name = "email"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def send(self, subject: str, body: str, short: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"[parking] {subject}"
        msg["From"] = self.cfg.smtp_from
        msg["To"] = ", ".join(self.cfg.alert_emails)
        msg.set_content(body + "\n\n-- cambridge-ma-parking-checks\n")
        with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(self.cfg.smtp_user, self.cfg.smtp_password)
            smtp.send_message(msg)


class AppsScriptEmailChannel:
    """Email through a Google Apps Script web app (deploy/apps_script_email.gs) over HTTPS.

    Useful on hosts that block outbound SMTP ports (many cloud providers do). The script checks
    a shared secret,
    only mails addresses on its own allowlist, and sends with MailApp as the script owner.
    """

    name = "email"

    def __init__(self, cfg: Config, session: requests.Session | None = None):
        self.cfg = cfg
        self.session = session or requests.Session()

    def send(self, subject: str, body: str, short: str) -> None:
        payload = {
            "secret": self.cfg.email_webhook_secret,
            "to": self.cfg.alert_emails,
            "subject": f"[parking] {subject}",
            "body": body + "\n\n-- cambridge-ma-parking-checks\n",
        }
        try:
            # Apps Script answers the POST with a 302 to the script's output; requests follows it.
            r = self.session.post(self.cfg.email_webhook_url, json=payload, timeout=30)
        except requests.RequestException as e:
            raise RuntimeError(f"email relay: {type(e).__name__}") from None
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"email relay: HTTP {r.status_code}, not JSON: {r.text[:120]!r}") from None
        if r.status_code != 200 or not data.get("ok"):
            raise RuntimeError(f"email relay: HTTP {r.status_code}: {data.get('error', data)}")


class TelegramChannel:
    name = "telegram"

    def __init__(self, cfg: Config, session: requests.Session | None = None):
        self.cfg = cfg
        self.session = session or requests.Session()

    def send(self, subject: str, body: str, short: str) -> None:
        text = f"{short}\n\n{body}" if body and body != short else short
        url = f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}/sendMessage"
        errors = []
        for chat_id in self.cfg.telegram_chat_ids:
            try:
                r = self.session.post(
                    url,
                    json={"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True},
                    timeout=20,
                )
                if r.status_code != 200:
                    errors.append(f"chat {chat_id}: HTTP {r.status_code} {r.text[:200]}")
            except requests.RequestException as e:
                # Never include the URL: it contains the bot token.
                errors.append(f"chat {chat_id}: {type(e).__name__}")
        if errors:
            raise RuntimeError("; ".join(errors))


def default_channels(cfg: Config) -> list[Channel]:
    chans: list[Channel] = []
    if cfg.email_webhook_enabled:
        chans.append(AppsScriptEmailChannel(cfg))
    elif cfg.email_enabled:
        chans.append(EmailChannel(cfg))
    if cfg.telegram_enabled:
        chans.append(TelegramChannel(cfg))
    return chans


class Notifier:
    def __init__(
        self,
        state: State,
        channels: list[Channel],
        *,
        dry_run: bool = False,
        clock: Callable[[], datetime],
        printer: Callable[[str], None] = print,
    ):
        self.state = state
        self.channels = channels
        self.dry_run = dry_run
        self.clock = clock
        self.printer = printer
        self.sent: list[tuple[str, str, str]] = []  # (kind, dedupe_key, subject) actually sent

    def already_sent(self, dedupe_key: str) -> bool:
        return self.state.alert_sent(dedupe_key)

    def notify(
        self, kind: str, subject: str, body: str, dedupe_key: str, short: str | None = None
    ) -> dict[str, str]:
        """Send to every channel. Returns {channel: sent|skipped|failed|dry-run}."""
        short = short or subject
        results: dict[str, str] = {}
        if self.dry_run:
            dup = " (would be skipped: already sent)" if self.state.alert_sent(dedupe_key) else ""
            self.printer(
                f"--- DRY RUN alert [{kind}] key={dedupe_key}{dup}\n"
                f"subject: {subject}\nshort:   {short}\n{body}\n---"
            )
            self.sent.append((kind, dedupe_key, subject))
            return {"dry-run": "dry-run"}
        if not self.channels:
            log.error("no alert channels configured; alert %s not delivered: %s", dedupe_key, subject)
            return {}
        delivered = False
        for ch in self.channels:
            if self.state.alert_sent(dedupe_key, ch.name):
                results[ch.name] = "skipped"
                continue
            try:
                ch.send(subject, body, short)
            except Exception as e:  # one channel failing must not block the others
                log.error("alert %s failed on %s: %s", dedupe_key, ch.name, e)
                results[ch.name] = "failed"
                continue
            self.state.mark_sent(dedupe_key, ch.name, self.clock())
            results[ch.name] = "sent"
            delivered = True
        if delivered:
            self.sent.append((kind, dedupe_key, subject))
        log.info("alert %s [%s]: %s", dedupe_key, kind, results)
        return results
