"""Deploy-time discovery of a Pond's Ripples, run in a subprocess under the Pond's own environment
(see plans/pond-environments.md), so the Catchment never imports Pond code itself.

The Catchment calls :func:`discover`, which runs ``python -m duckstring.discover IN OUT`` with the Pond's
interpreter. The subprocess imports the Pond's code (or parses its dbt project), captures static column
lineage, and writes the result as JSON to ``OUT``. It never writes to stdout, which Pond code may print to.

``IN``: ``{"source_dir": str, "catalog": {"source.table": [columns]}}``. ``catalog`` is the Source schemas
the lineage capture needs, since the subprocess has no Catchment database.

``OUT``: ``{"ok": true, "ripples": [{"name", "parents": [names], "always_run"}], "lineage": [[table, column,
kind, src_ref, src_column], ...]}``, or ``{"ok": false, "error": str, "traceback": str}``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

DEFAULT_TIMEOUT_S = 300


class DiscoveryError(RuntimeError):
    """A Pond's code couldn't be loaded or its dbt project parsed. ``detail`` holds the traceback."""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


# ─── the Catchment side ────────────────────────────────────────────────────────


def discover(python: str | Path, source_dir: Path, catalog: dict | None = None,
             timeout: float | None = None) -> dict:
    """Run discovery for the Pond in ``source_dir`` with the interpreter ``python``. Returns
    ``{"ripples": [...], "lineage": [...]}``; raises :class:`DiscoveryError` when the Pond can't be loaded."""
    timeout = timeout or float(os.environ.get("DUCKSTRING_DISCOVER_TIMEOUT", DEFAULT_TIMEOUT_S))
    with tempfile.TemporaryDirectory(prefix="duckstring-discover-") as tmp:
        inp, out = Path(tmp) / "in.json", Path(tmp) / "out.json"
        inp.write_text(json.dumps({"source_dir": str(Path(source_dir).resolve()), "catalog": catalog or {}}))
        try:
            proc = subprocess.run(
                [str(python), "-m", "duckstring.discover", str(inp), str(out)],
                cwd=source_dir, capture_output=True, text=True, timeout=timeout,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        except subprocess.TimeoutExpired:
            raise DiscoveryError(
                f"loading the Pond's code took longer than {timeout:.0f}s "
                "(set DUCKSTRING_DISCOVER_TIMEOUT to allow longer)"
            ) from None
        if not out.exists():  # the interpreter itself failed: no duckstring in the environment, a crash
            output = (proc.stderr or proc.stdout).strip()
            raise DiscoveryError("the Pond's environment couldn't run discovery", output[-4000:])
        result = json.loads(out.read_text())
    if not result.get("ok"):
        raise DiscoveryError(result.get("error", "discovery failed"), result.get("traceback", ""))
    return {"ripples": result["ripples"], "lineage": result.get("lineage", [])}


# ─── the subprocess side ───────────────────────────────────────────────────────


def capture_lineage_rows(ripples: list[dict], catalog: dict) -> list[list[str]]:
    """Static column lineage for each ``@ripple`` function: ``[table, column, kind, src_ref, src_column]``
    rows, ``kind`` ∈ exact/constant/opaque. Best-effort per Ripple: one that can't be captured (raw ``con``
    access, plain read/write code) contributes nothing, and nothing here ever raises (plans/lineage.md)."""
    try:
        from .trickle.capture import NonCapturable, capture_plan
        from .trickle.lineage import column_lineage
    except Exception:  # noqa: BLE001 — lineage is observability
        return []

    def source_catalog(ref: str) -> dict:
        entry = {"location": "__SRC__", "mode": None, "pk": None, "floor": None, "f": None}
        cols = catalog.get(ref)
        if cols:
            entry["schema"] = {c: "" for c in cols}
        return entry

    rows: list[list[str]] = []
    for r in ripples:
        func = r.get("func")
        if not callable(func):
            continue  # dbt-mode rows carry model-name strings
        try:
            body = capture_plan(lambda host, fn=func: fn(host), source_catalog=source_catalog)
            lineage = column_lineage(body)
        except NonCapturable:
            continue
        except Exception:  # noqa: BLE001 — a Ripple quirk must never fail lineage capture
            continue
        for table, cols in lineage.items():
            if cols is None:
                rows.append([table, "", "opaque", "", ""])
                continue
            for col, prov in cols.items():
                if prov is None:
                    rows.append([table, col, "opaque", "", ""])
                elif not prov:
                    rows.append([table, col, "constant", "", ""])
                else:
                    rows.extend([table, col, "exact", ref, sc] for ref, sc in sorted(prov))
    return rows


def _dbt_ripples(source_dir: Path, info: dict) -> list[dict]:
    from .dbt_mode import DbtError, manifest_to_ripples, parse_manifest, project_dir, write_profile

    proj = project_dir(source_dir, info)
    if not (proj / "dbt_project.yml").exists():
        raise DiscoveryError(f"dbt_project points at '{proj.name}/' but no dbt_project.yml is there")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            profiles = write_profile(Path(tmp), ":memory:")  # parse touches no registry
            return manifest_to_ripples(parse_manifest(proj, profiles))
    except DbtError as exc:
        raise DiscoveryError(str(exc)) from exc


def run(spec: dict) -> dict:
    """Discover the Pond described by ``spec`` (see the module docstring), in this process."""
    from .core import load_ripples, read_pond_toml
    from .dbt_mode import dbt_project_subpath

    source_dir = Path(spec["source_dir"])
    info = read_pond_toml(source_dir)
    if dbt_project_subpath(info):
        rows = _dbt_ripples(source_dir, info)  # names already, no functions
        return {"ripples": [{"name": r["name"], "parents": list(r["parents"]),
                             "always_run": bool(r.get("always_run"))} for r in rows], "lineage": []}

    # Python and SQL Ripples together, parents resolved and each SQL Ripple's query checked against its
    # declaration; a RippleDeclarationError here is a failed deploy with the reason.
    pond = load_ripples(source_dir, info)
    ripples = [{"name": r["name"], "parents": list(r["parents"]), "always_run": r["always_run"]}
               for r in pond.ripples]
    python_rows = [r for r in pond.ripples if r["kind"] == "python"]  # column lineage is captured for these
    return {"ripples": ripples, "lineage": capture_lineage_rows(python_rows, spec.get("catalog") or {})}


def main(argv: list[str]) -> int:
    inp, out = Path(argv[0]), Path(argv[1])
    try:
        result = {"ok": True, **run(json.loads(inp.read_text()))}
    except BaseException as exc:  # noqa: BLE001 — report every failure, including SystemExit from Pond code
        from .core import RippleDeclarationError

        # A declaration problem is the user's config, not a crash: its message says what to fix.
        tb = "" if isinstance(exc, RippleDeclarationError) else traceback.format_exc()
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "traceback": tb}
    out.write_text(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
