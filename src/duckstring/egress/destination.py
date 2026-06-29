"""Destination parsing/validation for a Spout (see plans/egress.md).

A destination is a URI whose **scheme** selects the egress driver (`file://`, `s3://`, `gs://`,
`postgres://`). Credentials travel inside it as ``${env:NAME}`` references, resolved only at egress time
(:mod:`duckstring.egress.credentials`) — :func:`parse_destination` validates the *syntax* (known scheme,
well-formed references) without resolving anything, so a Spout can be created before its secrets are
present in the environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from . import credentials

# Schemes a Spout may target. The concrete drivers are added with the egress worker; the construct
# accepts the destination ahead of that, so the config can be in place when execution lands.
OBJECT_STORE_SCHEMES = {"file", "s3", "gs"}
TRANSACTIONAL_SCHEMES = {"postgres", "postgresql"}
KNOWN_SCHEMES = OBJECT_STORE_SCHEMES | TRANSACTIONAL_SCHEMES

VALID_MODES = {"auto", "full", "append"}


class DestinationError(ValueError):
    """An invalid Spout destination or mode."""


@dataclass(frozen=True)
class Destination:
    scheme: str
    raw: str  # the original URI, with ${env:...} references intact (resolved only at egress time)

    @property
    def transactional(self) -> bool:
        """Whether the destination does identity-based upsert/delete (and so will require a primary
        key on the source — enforced when the transactional driver lands)."""
        return self.scheme in TRANSACTIONAL_SCHEMES


def parse_destination(uri: str) -> Destination:
    """Validate a Spout destination URI: a known scheme and well-formed ``${env:...}`` references.
    Does **not** resolve credentials. Raises :class:`DestinationError` on anything malformed."""
    if not uri or not uri.strip():
        raise DestinationError("destination must not be empty")
    try:
        credentials.references(uri)  # validates ${env:}/${secret:} syntax (raises on an empty reference)
    except credentials.CredentialError as exc:
        raise DestinationError(str(exc)) from exc
    scheme = urlparse(uri).scheme.lower()
    if not scheme:
        raise DestinationError(f"destination {uri!r} has no scheme — expected e.g. file://…, s3://…, postgres://…")
    if scheme not in KNOWN_SCHEMES:
        raise DestinationError(
            f"unsupported destination scheme {scheme!r} — supported: {', '.join(sorted(KNOWN_SCHEMES))}"
        )
    return Destination(scheme=scheme, raw=uri)


def validate_mode(mode: str) -> str:
    if mode not in VALID_MODES:
        raise DestinationError(f"unknown mode {mode!r} — use one of {', '.join(sorted(VALID_MODES))}")
    return mode
