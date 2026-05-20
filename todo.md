# Root — Python package setup

Run from this directory.

## Add FastAPI dependencies

Add a `catchment` optional-deps group to `pyproject.toml`:

```toml
[project.optional-dependencies]
catchment = [
  "fastapi>=0.111",
  "uvicorn[standard]>=0.29",
]
```

Then install:

```bash
pip install -e ".[catchment,dev]"
```

## Include static files in the wheel (if distributing)

Add to `pyproject.toml` so the built frontend is bundled:

```toml
[tool.setuptools.package-data]
"duckstring.catchment" = ["static/**/*"]
```

## Run the API locally

```bash
uvicorn duckstring.catchment.app:app --reload
```
