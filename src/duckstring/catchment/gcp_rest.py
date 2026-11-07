"""Google Cloud REST calls for the Cloud Run and Compute Engine backends, through google-auth's authorized
session (already in the ``gcp`` extra), rather than the gRPC client libraries: Google's were the heaviest extra
measured (plans/azure-gcp.md, Images), and the backends need a handful of calls each."""

from __future__ import annotations

import os
import time

CLOUD_PLATFORM = "https://www.googleapis.com/auth/cloud-platform"


class GcpError(RuntimeError):
    """A Google API call failed; the message is Google's own (it never carries a credential)."""

    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status


def default_session():
    """An authorized session from application default credentials, and the project they name (if any)."""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, project = google.auth.default(scopes=[CLOUD_PLATFORM])
    return AuthorizedSession(credentials), project


def project_id(explicit: str | None, session_project: str | None) -> str | None:
    return explicit or os.environ.get("GOOGLE_CLOUD_PROJECT") or session_project


def call(session, method: str, url: str, **kwargs) -> dict:
    """One request; the JSON body, ``{}`` for an empty one. Raises :class:`GcpError` on a non-2xx status."""
    resp = session.request(method, url, timeout=60, **kwargs)
    if resp.status_code >= 300:
        try:
            message = resp.json().get("error", {}).get("message") or resp.text
        except ValueError:
            message = resp.text
        raise GcpError(resp.status_code, (message or "").strip()[:400])
    return resp.json() if resp.content else {}


def wait_operation(session, url: str, timeout_s: float = 180.0) -> dict:
    """Poll a long-running operation at ``url`` until it is done; raises on its error or on timeout."""
    deadline = time.monotonic() + timeout_s
    while True:
        op = call(session, "GET", url)
        if op.get("done") or op.get("status") == "DONE":
            error = op.get("error")
            if error:
                raise GcpError(error.get("code", 500), error.get("message") or str(error))
            return op
        if time.monotonic() > deadline:
            raise GcpError(504, f"operation still running after {timeout_s:.0f} s: {url}")
        time.sleep(2.0)
