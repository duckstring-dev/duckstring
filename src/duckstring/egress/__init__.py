"""Egress: publishing a Pond's output to external systems (see plans/egress.md).

This package is being built incrementally. So far it holds **credential resolution** — how a Spout's
destination URI references a secret. The OSS posture is env-var-first: a destination stores a *reference*
like ``${env:PGPASSWORD}`` (never the plaintext), resolved from the process environment only at egress
time. See :mod:`duckstring.egress.credentials`.
"""

from __future__ import annotations

from .base import Capabilities, EgressDriver, get_egress, register
from .credentials import CredentialError, references, resolve
from .destination import Destination, DestinationError, parse_destination, validate_mode

__all__ = [
    "CredentialError", "references", "resolve",
    "Destination", "DestinationError", "parse_destination", "validate_mode",
    "Capabilities", "EgressDriver", "get_egress", "register",
]
