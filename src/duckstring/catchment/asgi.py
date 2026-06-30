"""ASGI entry for platform-hosted Catchments (Posit Connect, gunicorn/uvicorn, containers).

The platform owns the server lifecycle; this module just exposes the app. A deployable bundle is
two files — ``requirements.txt`` (``duckstring``) and an ``app.py`` containing::

    from duckstring.catchment.asgi import app

Configuration is environment-only:

- ``DUCKSTRING_STATE_ROOT`` (alias: ``DUCKSTRING_ROOT``) — the local POSIX root for **hot state**
  (``duck.db``, the ledgers, the working registries). Defaults to ``./.duckstring``. On a node with
  ephemeral local disk (Databricks Apps' ``/local_disk0``, a scale-to-zero container) this is lost on
  redeploy/scale-down — set ``DUCKSTRING_STATE_BACKUP_URI`` to make it durable.
- ``DUCKSTRING_DATA_ROOT`` — where the data plane publishes/reads tables: a local path **or** an
  object-store / Volume URI (``s3://…``, ``gs://…``, ``abfss://…``, ``/Volumes/…``). Credentials ride
  the URI query as ``${env:NAME}`` refs, resolved at runtime. Unset → under the state root (today's
  behaviour). An object-store data root auto-selects the parquet data plane.
- ``DUCKSTRING_STATE_BACKUP_URI`` — where Tier-1/2 state checkpoints sync (object store / Volume / path).
  Unset → no sync (single-node, non-ephemeral disk).
- ``DUCKSTRING_CHECKPOINT_INTERVAL`` — Tier-1 (``duck.db``) sync cadence, e.g. ``30s``. Default ``60s``.
- ``DUCKSTRING_API_KEY`` — optional built-in API key. Leave unset when the platform already gates
  requests (the recommended hosted model).
- ``DUCKSTRING_CATCHMENT_URL`` — optional Duck dial-back address. Normally unset: the platform
  picks the bind address, and the Catchment learns it from the first request it serves.

Run only **one** process of this app (e.g. Posit Connect's "Max processes" = 1): the Catchment is
a single brain — one scheduler, one SQLite, one set of Ducks. Single-writer-per-line is what makes the
object-store data plane safe without distributed locks, and a serial stop→start the only concurrency story.
"""

from __future__ import annotations

import os
from pathlib import Path

from .app import create_app

_state_root = os.environ.get("DUCKSTRING_STATE_ROOT") or os.environ.get("DUCKSTRING_ROOT") or ".duckstring"
app = create_app(Path(_state_root).expanduser().resolve())
