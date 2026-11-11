"""Catchment-level cloud config (plans/cloud-config.md, increment 2; plans/azure-gcp.md, phase 2).

The persisted ``catchment_setting`` key/value store (migration 021) plus the **cloud-enable gate**:
remote compute needs shared object storage a remote box can read, so the gate is *a data root on a
cloud that offers remote Ducks + that cloud's credentials present*. The data root's scheme picks the
Catchment's one cloud (:mod:`.providers`). The data-plane target is a **persisted setting** (settable
after the Catchment is made), but **set-once in practice**: switching it once data exists would strand
that data, so the setter refuses a populated Catchment (a migration is deferred).
"""

from __future__ import annotations

import sqlite3

from ..trickle_io import first_row
from . import providers

DATA_ROOT_KEY = "data_root"


# ─── the generic key/value setting store ─────────────────────────────────────────

def get_setting(con: sqlite3.Connection, key: str) -> str | None:
    row = first_row(con.execute("SELECT value FROM catchment_setting WHERE key = ?", (key,)))
    return row[0] if row else None


def set_setting(con: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        con.execute("DELETE FROM catchment_setting WHERE key = ?", (key,))
    else:
        con.execute(
            "INSERT INTO catchment_setting (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    con.commit()


def all_settings(con: sqlite3.Connection) -> dict[str, str]:
    return dict(con.execute("SELECT key, value FROM catchment_setting").fetchall())


def has_published_data(con: sqlite3.Connection) -> bool:
    """Whether the Catchment has ever published output — a successful Pond Run. The set-once guard
    keys off this: while it is False the data root is freely settable; once True, switching it would
    strand the published data, so the setter refuses."""
    row = first_row(con.execute("SELECT 1 FROM pond_run WHERE status = 'success' LIMIT 1"))
    return row is not None


# ─── the cloud-enable gate ──────────────────────────────────────────────────────

def is_remote(data_root: str | None) -> bool:
    """A data root on a cloud (s3://, az://, abfss://, gs://), which a remote box could read. A local path /
    None means only Catchment Ducks are launchable (no shared storage)."""
    return providers.for_data_root(data_root) is not None


def compute_enabled(data_root: str | None, secret_store=None) -> bool:
    """Whether remote Ducks can be launched: the data root's cloud offers them and its credentials are present."""
    p = providers.for_data_root(data_root)
    return bool(p is not None and p.compute and p.credentials_present(secret_store))


def load_env(secret_store=None) -> None:
    """Load every cloud's credential secrets into the environment (real environment variables win)."""
    for p in providers.providers().values():
        p.load_env(secret_store)


def cloud_status(data_root: str | None, secret_store=None, cred_status: dict | None = None) -> dict:
    """The gate + its reasons — surfaced on /api/status and the settings endpoint so the UI can grey
    out remote-compute options and explain why. ``provider`` is the data root's cloud (None for a local
    root), ``compute_available`` whether that cloud offers remote Ducks yet, and ``backends`` each backend's
    readiness: cloud enabled AND the backend's env deployment config is adequate to launch WITHOUT
    per-pool input (Fargate's readiness gates the S/M/L/XL presets on AWS). ``cred_status`` (the cached
    live-validation result, see cloud_backends) adds ``creds_valid``/``creds_error`` so the UI can
    persistently warn when the gate is enabled but the credentials don't authenticate (``creds_valid is
    None`` = not yet checked / gate off → no warning). ``aws_configured``, ``fargate_enabled`` and
    ``ec2_enabled`` stay for the UI until it's provider-aware."""
    from . import cloud_deploy

    p = providers.for_data_root(data_root)
    creds = bool(p is not None and p.credentials_present(secret_store))
    enabled = bool(creds and p.compute)
    backends = {b: bool(enabled and not cloud_deploy.missing_fields(b, None)) for b in (p.backends if p else ())}
    out = {
        "data_root": data_root,
        "data_root_remote": p is not None,
        "provider": p.name if p else None,
        "provider_title": p.title if p else None,
        "compute_available": bool(p and p.compute),
        "credentials_configured": creds,
        "aws_configured": providers.get("aws").credentials_present(secret_store),
        "cloud_enabled": enabled,
        "backends": backends,
        "fargate_enabled": backends.get("fargate", False),
        "ec2_enabled": backends.get("ec2", False),
        "creds_valid": None,
        "creds_error": None,
    }
    if cred_status is not None:
        out["creds_valid"] = cred_status.get("valid")
        out["creds_error"] = cred_status.get("error")
    return out
