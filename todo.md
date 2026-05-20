# Root — Python package setup

Run from this directory.

## Add FastAPI dependencies

Add fastapi and uvicorn as hard dependencies (not optional extras) so that
`pip install duckstring` always works out of the box. Optional extras only make
sense for heavy or narrow-use deps — fastapi/uvicorn are lightweight and central
to the package's purpose.

```toml
[project]
dependencies = [
  # ... existing deps ...
  "fastapi>=0.111",
  "uvicorn[standard]>=0.29",
]
```

Then install:

```bash
pip install -e ".[dev]"
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
