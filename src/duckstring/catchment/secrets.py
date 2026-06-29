"""The write-only Catchment secret store (see plans/egress.md "Secrets").

A minimal, **write-only** credential store at the Catchment root: an operator can `set`/`rm` a secret and
`list` the *names*, but there is **no read-back** — values are returned only internally, to resolve a
``${secret:NAME}`` reference at egress time. Plaintext, ``chmod 0600`` (the same posture as ``config.toml``),
and **excluded from the archive** (`catchment download` skips it — see ``routes/catchment.py``), so secrets
never travel in a state bundle.

This is the runtime, no-SSH path: set a credential against a live Catchment and reference it from a Spout
destination as ``${secret:NAME}``. (The env-var path — ``${env:NAME}`` — stays the platform-native default;
this is the convenience for provisioning a credential without redeploying. The ``set`` request transmits
the value, so use it over HTTPS.)
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

SECRETS_FILE = "secrets.json"  # at the Catchment root; EXCLUDED from the archive walk
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")  # a clean ${secret:NAME} reference (env-var-like)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SecretStore:
    def __init__(self, root: Path):
        self.path = Path(root) / SECRETS_FILE

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    def _save(self, data: dict[str, dict]) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, self.path)  # atomic: a concurrent reader sees old-or-new, never partial

    def set(self, name: str, value: str) -> None:
        if not _NAME.fullmatch(name):
            raise ValueError(f"invalid secret name {name!r} — use letters, digits and underscores (env-var style)")
        data = self._load()
        data[name] = {"set_at": _now()}  # store under a value key separate from metadata
        data[name]["value"] = value
        self._save(data)

    def names(self) -> list[dict]:
        """Names + set time only — never values."""
        return [{"name": n, "set_at": v.get("set_at")} for n, v in sorted(self._load().items())]

    def get(self, name: str) -> str | None:
        """The value, for credential resolution only (never exposed by the API)."""
        v = self._load().get(name)
        return v.get("value") if v else None

    def remove(self, name: str) -> bool:
        data = self._load()
        if name not in data:
            return False
        del data[name]
        self._save(data)
        return True
