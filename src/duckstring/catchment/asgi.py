"""ASGI entry for platform-hosted Catchments (Posit Connect, gunicorn/uvicorn, containers).

The platform owns the server lifecycle; this module just exposes the app. A deployable bundle is
two files — ``requirements.txt`` (``duckstring``) and an ``app.py`` containing::

    from duckstring.catchment.asgi import app

Configuration is environment-only:

- ``DUCKSTRING_ROOT`` — the Catchment root. Defaults to ``./.duckstring`` (relative to the working
  directory, i.e. the deployed content directory). On platforms that replace the content directory
  on redeploy (Posit Connect does), the default survives process restarts but **not redeploys of
  the Catchment app itself** — point this at a persistent path for durable state.
- ``DUCKSTRING_API_KEY`` — optional built-in API key. Leave unset when the platform already gates
  requests (the recommended hosted model).
- ``DUCKSTRING_CATCHMENT_URL`` — optional Duck dial-back address. Normally unset: the platform
  picks the bind address, and the Catchment learns it from the first request it serves.

Run only **one** process of this app (e.g. Posit Connect's "Max processes" = 1): the Catchment is
a single brain — one scheduler, one SQLite, one set of Ducks.
"""

from __future__ import annotations

import os
from pathlib import Path

from .app import create_app

app = create_app(Path(os.environ.get("DUCKSTRING_ROOT", ".duckstring")).expanduser().resolve())
