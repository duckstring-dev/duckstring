"""Reaching a Catchment that only lets Google identities in (plans/azure-gcp.md, phase 4).

A Catchment on Cloud Run can stay private: Cloud Run then admits only requests carrying a Google identity token
for the service. A Duck (or Pool agent) on Cloud Run or Compute Engine has one from its service account, through
the metadata server, so it sends it in ``X-Serverless-Authorization`` (Cloud Run checks that header and passes it
on, leaving ``Authorization`` to the app; the Duck channel authenticates with ``X-Duck-Token``). The operator
grants the Duck's service account ``roles/run.invoker`` on the service.

The token is sent when the Catchment's URL is a Cloud Run address (``*.run.app``), or when an audience is named
for a custom domain in front of the service: ``DUCKSTRING_CATCHMENT_AUDIENCE`` set on the Catchment, which its
launchers pass to each Duck and Pool agent as ``--audience`` (cloud Ducks don't inherit the Catchment's env).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

HEADER = "X-Serverless-Authorization"
_MIN_LIFE_S = 5 * 60  # a token is re-fetched with less than this left (they live an hour)


AUDIENCE_ENV = "DUCKSTRING_CATCHMENT_AUDIENCE"


def audience_args() -> list[str]:
    """The ``--audience`` argument a launcher gives a Duck or Pool agent: the Catchment's
    ``DUCKSTRING_CATCHMENT_AUDIENCE``, or nothing when it isn't set."""
    audience = os.environ.get(AUDIENCE_ENV)
    return [f"--audience={audience}"] if audience else []


def audience_for(base_url: str, audience: str | None = None) -> str | None:
    """The identity-token audience for this Catchment URL, or None when it needs no token: ``audience`` (the
    ``--audience`` argument), else ``DUCKSTRING_CATCHMENT_AUDIENCE``, else the URL itself for a ``*.run.app``
    address."""
    explicit = audience or os.environ.get(AUDIENCE_ENV)
    if explicit:
        return explicit
    parts = urlsplit(base_url)
    if (parts.hostname or "").endswith(".run.app"):
        return f"{parts.scheme}://{parts.netloc}"
    return None


def _expiry(token: str) -> float:
    try:
        payload = token.split(".")[1]
        return float(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"])
    except Exception:
        return time.time() + 600  # unreadable: assume ten minutes, so it's refreshed soon


def _fetch(audience: str) -> str:
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    return id_token.fetch_id_token(Request(), audience)


class IdTokenAuth(httpx.Auth):
    """An httpx auth hook adding a cached Google identity token for ``audience`` to every request."""

    def __init__(self, audience: str, fetch=_fetch):
        self.audience = audience
        self._fetch = fetch
        self._token: str | None = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def token(self) -> str | None:
        with self._lock:
            if self._token is None or self._expires - time.time() < _MIN_LIFE_S:
                try:
                    self._token = self._fetch(self.audience)
                    self._expires = _expiry(self._token)
                except Exception as exc:
                    # Without a token the Catchment answers 403, which the Duck already treats as no contact.
                    log.warning("could not get a Google identity token for %s: %s", self.audience, exc)
                    return self._token
            return self._token

    def auth_flow(self, request):
        token = self.token()
        if token:
            request.headers[HEADER] = f"Bearer {token}"
        yield request


def catchment_auth(base_url: str, audience: str | None = None) -> IdTokenAuth | None:
    """The auth hook for a Duck's client to this Catchment, or None when it needs none."""
    audience = audience_for(base_url, audience)
    return IdTokenAuth(audience) if audience else None
