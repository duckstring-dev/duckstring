"""The email notifier (``mailto:``) — the simplest floor.

``mailto:ops@x.com,dev@x.com?smtp=host:587&from=alerts@x.com&user=${env:SMTP_USER}&password=${secret:SMTP_PASS}&tls=1``

SMTP host/port/user/password/from come from the URI query, or the ``DUCKSTRING_SMTP_*`` environment as a
fallback (so a Catchment can carry one SMTP config for every mailto channel). Credentials are resolved from
``${env:}``/``${secret:}`` only at send time. Uses the stdlib ``smtplib`` — no new dependency.
"""

from __future__ import annotations

import os
from email.message import EmailMessage
from urllib.parse import parse_qs, unquote, urlparse

from .base import Destination, NotifierError
from .event import AlertEvent

_TIMEOUT = 20.0


class EmailNotifier:
    SCHEMES = ("mailto",)

    def __init__(self, dest: Destination):
        parsed = urlparse(dest.raw)
        self.recipients = [r.strip() for r in unquote(parsed.path).split(",") if r.strip()]
        if not self.recipients:
            raise NotifierError("mailto: destination has no recipient — use mailto:you@example.com")
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self._raw = q  # references intact; resolved at send time
        self.smtp = q.get("smtp") or os.environ.get("DUCKSTRING_SMTP_HOST", "")
        self.sender = q.get("from") or os.environ.get("DUCKSTRING_SMTP_FROM", "duckstring@localhost")
        tls = q.get("tls")
        self.tls = (tls not in ("0", "false", "no")) if tls is not None else \
            (os.environ.get("DUCKSTRING_SMTP_TLS", "1") not in ("0", "false", "no"))
        if not self.smtp:
            raise NotifierError(
                "mailto: destination needs an SMTP server — add ?smtp=host:port or set DUCKSTRING_SMTP_HOST"
            )

    def _host_port(self) -> tuple[str, int]:
        host, _, port = self.smtp.partition(":")
        return host, int(port) if port else 587

    def _credentials(self) -> tuple[str | None, str | None]:
        from ..egress import credentials

        user = self._raw.get("user") or os.environ.get("DUCKSTRING_SMTP_USER")
        password = self._raw.get("password") or os.environ.get("DUCKSTRING_SMTP_PASSWORD")
        # Resolve ${env:}/${secret:} refs at call time — never persisted/logged resolved.
        user = credentials.resolve(user) if user else None
        password = credentials.resolve(password) if password else None
        return user, password

    def _connect(self):
        import smtplib

        host, port = self._host_port()
        server = smtplib.SMTP(host, port, timeout=_TIMEOUT)
        try:
            server.ehlo()
            if self.tls:
                server.starttls()
                server.ehlo()
            user, password = self._credentials()
            if user and password:
                server.login(user, password)
        except Exception:
            server.close()
            raise
        return server

    def send(self, event: AlertEvent) -> None:
        msg = EmailMessage()
        msg["Subject"] = event.summary()
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        lines = [event.message]
        if event.pond:
            lines.append(f"\nPond: {event.pond}")
        if event.f:
            lines.append(f"Freshness: {event.f}")
        if event.detail:
            for k, v in event.detail.items():
                lines.append(f"{k}: {v}")
        msg.set_content("\n".join(lines))
        try:
            server = self._connect()
            try:
                server.send_message(msg)
            finally:
                server.quit()
        except NotifierError:
            raise
        except Exception as exc:  # noqa: BLE001 — sanitise: type only, never the credential/host detail
            raise NotifierError(f"email delivery failed: {type(exc).__name__}") from None

    def test(self) -> None:
        try:
            server = self._connect()  # connect + STARTTLS + login, deliver nothing
            server.quit()
        except NotifierError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise NotifierError(f"SMTP connection failed: {type(exc).__name__}") from None
