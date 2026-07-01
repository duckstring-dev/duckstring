"""The notifier seam (see plans/alerts.md) — mirrors :mod:`duckstring.egress.base`.

A small, scheme-selected interface the alert worker delivers an :class:`AlertEvent` through. The channel
destination is a URI whose scheme picks the notifier; ``get_notifier(destination)`` resolves it, exactly
like ``get_egress``. Credentials travel inside the URI as ``${env:NAME}``/``${secret:NAME}`` references and
are resolved only at send time (:mod:`duckstring.egress.credentials`) — never persisted or logged resolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable
from urllib.parse import urlparse

from ..egress import credentials
from .event import AlertEvent

# Schemes a channel may target. http/https → a webhook (Slack-incoming-webhook compatible); mailto → SMTP.
WEBHOOK_SCHEMES = {"http", "https"}
EMAIL_SCHEMES = {"mailto"}
KNOWN_SCHEMES = WEBHOOK_SCHEMES | EMAIL_SCHEMES


class NotifierError(ValueError):
    """An invalid channel destination, or a delivery/connectivity failure (sanitised — never a credential)."""


@dataclass(frozen=True)
class Destination:
    scheme: str
    raw: str  # the original URI, with ${...} references intact (resolved only at send time)


def parse_notifier_destination(uri: str) -> Destination:
    """Validate a channel destination URI: a known scheme + well-formed ``${...}`` references. Does **not**
    resolve credentials (so a channel can be created before its secrets are present). Raises NotifierError."""
    if not uri or not uri.strip():
        raise NotifierError("destination must not be empty")
    try:
        credentials.references(uri)  # validates ${env:}/${secret:} syntax
    except credentials.CredentialError as exc:
        raise NotifierError(str(exc)) from exc
    scheme = urlparse(uri).scheme.lower()
    if not scheme:
        raise NotifierError(f"destination {uri!r} has no scheme — expected e.g. https://…, mailto:…")
    if scheme not in KNOWN_SCHEMES:
        raise NotifierError(
            f"unsupported alert destination scheme {scheme!r} — supported: {', '.join(sorted(KNOWN_SCHEMES))}"
        )
    return Destination(scheme=scheme, raw=uri)


@runtime_checkable
class Notifier(Protocol):
    def send(self, event: AlertEvent) -> None:
        """Deliver ``event`` to the destination. Raises :class:`NotifierError` (sanitised) on failure."""
        ...

    def test(self) -> None:
        """Probe connectivity/credentials without delivering a real alert (the ``alert test`` command).
        Returns on success; raises :class:`NotifierError` (sanitised) on failure."""
        ...


_REGISTRY: dict[str, Callable[[Destination], Notifier]] = {}


def register(scheme: str, factory: Callable[[Destination], Notifier]) -> None:
    _REGISTRY[scheme] = factory


def get_notifier(destination: str) -> Notifier:
    """Resolve the notifier for a channel destination by its scheme. Raises :class:`NotifierError` for an
    unknown scheme, or a known scheme whose notifier is not built."""
    dest = parse_notifier_destination(destination)
    factory = _REGISTRY.get(dest.scheme)
    if factory is None:
        raise NotifierError(
            f"notifier for scheme {dest.scheme!r} is not implemented yet (built: "
            f"{', '.join(sorted(_REGISTRY)) or 'none'})"
        )
    return factory(dest)


def _register_builtins() -> None:
    from .email import EmailNotifier
    from .webhook import WebhookNotifier

    for driver in (WebhookNotifier, EmailNotifier):
        for scheme in driver.SCHEMES:
            register(scheme, driver)


_register_builtins()
