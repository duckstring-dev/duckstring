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
from collections.abc import Callable, Mapping
from dataclasses import dataclass

# ${env:NAME} / ${secret:NAME} — the name is any run of non-`}` chars (trimmed); other schemes don't match.
_REF = re.compile(r"\$\{(env|secret):([^}]*)\}")


class CredentialError(Exception):
    """A credential reference could not be resolved (a missing env var or unset secret)."""


# The Catchment injects its write-only secret store here (process-wide) so ``${secret:NAME}`` resolves
# deep inside the egress drivers without threading it through every call. A ``name -> value | None``
# lookup; ``None`` when no store is attached (e.g. tests, or a Catchment that never set one).
_secret_provider: "Callable[[str], str | None] | None" = None


def set_secret_provider(fn: "Callable[[str], str | None] | None") -> None:
    global _secret_provider
    _secret_provider = fn


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


def resolve(
    text: str, *, env: Mapping[str, str] | None = None,
    secret: "Callable[[str], str | None] | None" = None,
) -> str:
    """Substitute every ``${env:NAME}`` (from the environment) and ``${secret:NAME}`` (from the Catchment
    secret store) in ``text`` with its value. Raises :class:`CredentialError` naming an unset var/secret
    (never a value). ``secret`` overrides the injected provider (for tests). **Do not log or persist the
    result.**"""
    environ = os.environ if env is None else env
    secret_lookup = secret if secret is not None else _secret_provider

    def _sub(match: re.Match[str]) -> str:
        scheme, name = match.group(1), match.group(2).strip()
        if not name:
            raise CredentialError(f"empty credential reference: ${{{scheme}:}}")
        if scheme == "secret":
            val = secret_lookup(name) if secret_lookup is not None else None
            if val is None:
                raise CredentialError(
                    f"secret {name!r} is not set (referenced as ${{secret:{name}}} — set it with "
                    "`duckstring secret set` or the UI, or use ${{env:…}})"
                )
            return val
        try:
            return environ[name]
        except KeyError:
            raise CredentialError(f"environment variable {name!r} is not set (referenced as ${{env:{name}}})") from None

    return _REF.sub(_sub, text)
