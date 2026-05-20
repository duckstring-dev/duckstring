# catchment/ — FastAPI app

Run from the repo root (not this directory).

## Files to create

- `__init__.py` — expose the app for import
- `app.py` — FastAPI instance, mounts static files, registers routers
- `routes/__init__.py` — route registration
- `routes/catchment.py` — Catchment API endpoints

## Serve the built frontend

In `app.py`, mount the static directory so FastAPI serves the Next.js export:

```python
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

app = FastAPI()

static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")
```

Mount the API routes *before* the static catch-all, or use a prefix like `/api`.

## static/ directory

`static/` is where the CI pipeline drops the Next.js build output.
It is gitignored — do not commit build artifacts here.
Create it manually for local dev if needed:

```bash
mkdir -p src/duckstring/catchment/static
```
