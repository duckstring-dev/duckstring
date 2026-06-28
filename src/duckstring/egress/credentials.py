"""Credential resolution for egress destinations — env-var-first (see plans/egress.md "Secrets").

A Spout's destination stores a credential **reference**, never the plaintext: a URI like
``postgres://user:${env:PGPASSWORD}@host/db`` or ``s3://bucket/prefix?key=${env:AWS_SECRET}``. The
reference is what we persist, display (``spout ls``), and log — it is safe. It is resolved to the real
value **only at egress time**, from the process environment, by :func:`resolve` — and the resolved
string must never be persisted or logged.

Two reference schemes:

- ``${env:NAME}`` — resolved from ``os.environ[NAME]`` at call time. The blessed OSS path: the host
  platform (systemd, docker, k8s, Posit Connect, the cloud hosts) injects secrets as env vars.
- ``${secret:NAME}`` — **reserved** for the write-only secret store (a documented fast-follow). Parsed
  and reported by :func:`references` so a pre-flight can name it, but :func:`resolve` raises until the
  store exists — so the syntax is stable now and adding the store later breaks nothing.

Anything else (a bare ``${HOME}``, ``$FOO``) is left untouched — only the two recognised schemes are
interpolated, so a string that merely contains ``${...}`` is never mangled.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

# ${env:NAME} / ${secret:NAME} — the name is any run of non-`}` chars (trimmed); other schemes don't match.
_REF = re.compile(r"\$\{(env|secret):([^}]*)\}")


class CredentialError(Exception):
    """A credential reference could not be resolved (missing env var, or a reserved scheme)."""


@dataclass(frozen=True)
class Reference:
    scheme: str  # 'env' | 'secret'
    name: str


def references(text: str) -> list[Reference]:
    """Every credential reference in ``text``, in order (duplicates kept) — for pre-flight validation
    (e.g. "this Spout needs PGPASSWORD"). Never resolves, so it is safe on an unresolvable string."""
    out: list[Reference] = []
    for scheme, raw in _REF.findall(text):
        name = raw.strip()
        if not name:
            raise CredentialError(f"empty credential reference: ${{{scheme}:}}")
        out.append(Reference(scheme=scheme, name=name))
    return out


def resolve(text: str, *, env: Mapping[str, str] | None = None) -> str:
    """Substitute every ``${env:NAME}`` in ``text`` with the environment value, returning the resolved
    string. Raises :class:`CredentialError` if a referenced var is unset (naming the var, never a value)
    or if a reserved ``${secret:...}`` reference is used. **Do not log or persist the result.**"""
    environ = os.environ if env is None else env

    def _sub(match: re.Match[str]) -> str:
        scheme, name = match.group(1), match.group(2).strip()
        if not name:
            raise CredentialError(f"empty credential reference: ${{{scheme}:}}")
        if scheme == "secret":
            raise CredentialError(
                f"secret references (${{secret:{name}}}) are not yet supported — use ${{env:{name}}} "
                "and inject the value via the environment"
            )
        try:
            return environ[name]
        except KeyError:
            raise CredentialError(f"environment variable {name!r} is not set (referenced as ${{env:{name}}})") from None

    return _REF.sub(_sub, text)
