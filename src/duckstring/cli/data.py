"""Reading and managing what Ponds publish: the top-level `query` and `get`, and the `data` group.

Tables and Objects are named the way the Python API names them, ``POND.NAME`` (backticks around a part that
contains dots)::

    duckstring query reports.monthly_summary             # its first rows
    duckstring query "SELECT * FROM reports.monthly_summary WHERE year = 2026"
    duckstring get reports.monthly_summary               # a table's files, or an Object
    duckstring data ls reports
    duckstring data delete reports.monthly_summary
    duckstring data expose reports.monthly_summary --on
    duckstring data lineage reports.monthly_summary      # (cli/lineage.py)
    duckstring data trace reports.monthly_summary --where "year = 2026"
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(help="List, delete and expose published tables and Objects, and trace where they came from.",
                  no_args_is_help=True)

_CATCHMENT = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted).")
_MAJOR = typer.Option(None, "--major", "-m", help="Major version to target (default: latest).")
_VERSION = typer.Option(None, "--version", "-v", help="Specific semver to target, e.g. 1.2.3.")
_REF_HELP = "The table or Object, as POND.NAME (backticks around a part that contains dots)."
_IS_TABLE = typer.Option(False, "--table", help="It's a table (when the Pond has a table and an Object of that name).")
_IS_OBJECT = typer.Option(False, "--object", help="It's an Object (when the Pond has a table and an Object of that "
                                                  "name).")
# A bare reference, POND.NAME: one token, no spaces, each part plain or backticked.
_REF_RE = re.compile(r"^(`[^`]+`|[^\s.`]+)\.(`[^`]+`|[^\s`]+)$")


def _cfg(catchment: Optional[str]) -> dict:
    from .config import resolve_catchment

    return resolve_catchment(catchment)[1]


def split_ref(ref: str, what: str = "a table or Object") -> tuple[str, str]:
    """``POND.NAME`` → ``(pond, name)``. Exits with a message for anything else."""
    from duckstring.core import parse_ref

    try:
        pond, name = parse_ref(ref)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    if pond is None:
        raise typer.BadParameter(f"name {what} as POND.NAME, e.g. reports.monthly_summary")
    return pond, name


def asset_kind(cfg: dict, pond: str, name: str, params: dict, table: bool, obj: bool) -> str:
    """Whether ``pond.name`` is a ``"table"`` or an ``"object"``. ``--table``/``--object`` decide when the Pond
    has both; a companion of a Trickle table (``X__changelog``) counts as its table."""
    from duckstring.trickle_io import base_table_name

    from . import _http

    if table and obj:
        raise typer.BadParameter("give --table or --object, not both")
    if table:
        return "table"
    if obj:
        return "object"
    url = cfg["url"]
    tables = {t["name"] for t in _http.get(f"{url}/api/ponds/{pond}/tables", auth=cfg, params=params).json()
              .get("tables", [])}
    objects = {o["name"] for o in _http.get(f"{url}/api/ponds/{pond}/objects", auth=cfg, params=params).json()
               .get("objects", [])}
    is_table = name in tables or base_table_name(name) in tables
    is_object = name in objects
    if is_table and is_object:
        raise typer.BadParameter(f"'{pond}' has a table and an Object named '{name}': add --table or --object")
    if is_table:
        return "table"
    if is_object:
        return "object"
    raise typer.BadParameter(f"'{pond}' has no table or Object named '{name}'")


def _print_rows(columns: list[str], rows: list[list]) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    if not rows:
        console.print("[dim]No results.[/dim]")
        return
    table = Table(show_header=True, header_style="bold dim")
    for col in columns:
        table.add_column(str(col))
    for row in rows:
        table.add_row(*("" if v is None else str(v) for v in row))
    console.print(table)


# ─── query and get (top level) ─────────────────────────────────────────────────


def query(
    target: str = typer.Argument(..., metavar="SQL | POND.TABLE",
                                 help="SQL (or @path/to/file.sql), or a table to show its first rows."),
    pond: Optional[str] = typer.Option(None, "--pond", "-p", help="Run the SQL against this Pond's tables by bare "
                                                                  "name, instead of across the catalog."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
    limit: int = typer.Option(1000, "--limit", "-n", help="Rows to print at most (files are written in full)."),
    csv_out: Optional[str] = typer.Option(None, "--csv", metavar="FILENAME", help="Write the result as CSV."),
    json_out: Optional[str] = typer.Option(None, "--json", metavar="FILENAME", help="Write the result as JSON."),
    parquet_out: Optional[str] = typer.Option(None, "--parquet", metavar="FILENAME", help="Write the result as Parquet."),
    path: Optional[Path] = typer.Option(None, "--path", help="Directory for the file (default: ./ponds/{pond}/{table} "
                                                             "for a table, ./ponds/{pond} with --pond, else here)."),
) -> None:
    """Run read-only SQL, and print or save the result.

    SQL runs across the catalog, where each Pond is a schema: `reports` for its served major, `reports_v2` for a
    given one. With --pond, it runs against that Pond's tables by bare name. A table, POND.TABLE, shows its
    first 10 rows.
    """
    from . import _http

    cfg = _cfg(catchment)
    url = cfg["url"]
    out_name = csv_out or json_out or parquet_out
    fmt = "csv" if csv_out else "json" if json_out else "parquet" if parquet_out else None

    table_ref = None
    sql = target
    if target.startswith("@"):
        sql_file = Path(target[1:])
        if not sql_file.exists():
            typer.echo(f"Error: SQL file not found: {sql_file}", err=True)
            raise typer.Exit(1)
        sql = sql_file.read_text(encoding="utf-8")
    elif _REF_RE.match(target):
        table_ref = split_ref(target, "a table")

    if table_ref or pond:
        # One Pond's tables, by bare name (its published snapshot).
        name = table_ref[0] if table_ref else pond
        payload: dict = {"pond": name, **_http.pond_params(major, version)}
        if table_ref:
            payload["ripple"] = table_ref[1]
        else:
            payload["sql"] = sql
        if fmt:
            payload["format"] = fmt
        resp = _http.post(f"{url}/api/query", auth=cfg, json=payload)
        default_dir = Path("ponds") / name / table_ref[1] if table_ref else Path("ponds") / name
        if not fmt:
            rows = resp.json() or []
            cols = list(rows[0].keys()) if rows else []
            _print_rows(cols, [list(r.values()) for r in rows[:limit]])
            return
    else:
        body = {"sql": sql, "limit": 0 if fmt else limit}
        if fmt:
            body["format"] = fmt
        resp = _http.post(f"{url}/api/serve/query", auth=cfg, json=body)
        default_dir = Path(".")
        if not fmt:
            res = resp.json()
            _print_rows(res["columns"], res["rows"])
            return
    out_path = Path(path or default_dir) / out_name
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(resp.content)
    typer.echo(f"Written to {out_path}")


def get(
    ref: str = typer.Argument(..., metavar="POND.NAME", help=_REF_HELP),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Where to write it (default: ./ponds/{pond}/{table} "
                                                                 "for a table, ./{name} for an Object)."),
    table: bool = _IS_TABLE,
    obj: bool = _IS_OBJECT,
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
) -> None:
    """Download a table's published files, or an Object. A directory Object is unpacked into a folder."""
    from . import _http

    pond, name = split_ref(ref)
    cfg = _cfg(catchment)
    params = _http.pond_params(major, version)
    kind = asset_kind(cfg, pond, name, params, table, obj)
    typer.echo(f"Fetching {pond}.{name}...")
    if kind == "table":
        resp = _http.get(f"{cfg['url']}/api/ponds/{pond}/ripples/{name}", auth=cfg, params=params)
        dest = Path(out or Path("ponds") / pond / name)
        dest.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                zf.extractall(dest)
        except zipfile.BadZipFile:
            (dest / "output.bin").write_bytes(resp.content)
        typer.echo(f"Written to {dest}")
        return
    resp = _http.get(f"{cfg['url']}/api/ponds/{pond}/objects/{name}", auth=cfg, params=params)
    dest = Path(out or name)
    if resp.headers.get("content-type", "").startswith("application/zip"):  # a directory Object
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            zf.extractall(dest)
        typer.echo(f"Written to {dest}/")
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        typer.echo(f"Written to {dest}")


# ─── the data group ────────────────────────────────────────────────────────────


@app.command("ls")
def ls(
    pond: Optional[str] = typer.Argument(None, help="The Pond whose tables and Objects to list (default: every Pond)."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
) -> None:
    """List each Pond's published tables and Objects, with whether each table is exposed and when it last changed."""
    from rich.console import Console
    from rich.table import Table

    from . import _http

    cfg = _cfg(catchment)
    url = cfg["url"]
    cat = _http.get(f"{url}/api/serve", auth=cfg).json()
    ponds = [p for p in cat.get("ponds", []) if pond is None or p["name"] == pond]
    if pond is not None and not ponds:
        raise typer.BadParameter(f"no Pond named '{pond}' with published tables")
    console = Console()
    if not ponds:
        console.print("[dim]No published tables.[/dim]")
        return
    for p in ponds:
        m = major if major is not None else p["served_major"]
        try:
            fr = _http.get(f"{url}/api/ponds/{p['name']}/freshness", auth=cfg, params={"major": m}).json()
        except typer.Exit:
            fr = {}
        changed = {**(fr.get("tables") or {}), **{f"\0{k}": v for k, v in (fr.get("objects") or {}).items()}}
        majors = ", ".join(str(x) for x in p["majors"])
        console.print(f"[bold]{p['name']}[/bold] v{m}  [dim](majors {majors}; served v{p['served_major']})[/dim]")
        table = Table(show_header=True, header_style="bold dim", box=None, padding=(0, 1))
        for col in ("Name", "Kind", "Exposed", "Changed (UTC)"):
            table.add_column(col)
        for t in sorted((t for t in p["tables"] if t["major"] == m), key=lambda t: t["table"]):
            exposed = "yes" if t.get("exposed") else "[dim]no[/dim]"
            source = f" [dim]({t['source']})[/dim]" if t.get("source") and t["source"] != "declared" else ""
            table.add_row(t["table"], "table", exposed + source, _short(changed.get(t["table"])))
        for name, f in sorted((k[1:], v) for k, v in changed.items() if k.startswith("\0")):
            table.add_row(name, "Object", "", _short(f))
        console.print(table)


def _short(iso: Optional[str]) -> str:
    if not iso:
        return ""
    from datetime import datetime, timezone

    return datetime.fromisoformat(iso).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


@app.command("delete")
def delete(
    ref: str = typer.Argument(..., metavar="POND.NAME", help=_REF_HELP),
    table: bool = _IS_TABLE,
    obj: bool = _IS_OBJECT,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
) -> None:
    """Delete a table (its published data and its state in the Pond's working database) or an Object.

    It reappears only if the Pond's code writes it again. Deleting a table needs the Pond idle.
    """
    from duckstring.trickle_io import base_table_name

    from . import _http

    pond, name = split_ref(ref)
    cfg = _cfg(catchment)
    params = _http.pond_params(major, version)
    kind = asset_kind(cfg, pond, name, params, table, obj)
    if kind == "object":
        if not yes:
            typer.confirm(f"Delete Object '{pond}.{name}'?", abort=True)
        _http.delete(f"{cfg['url']}/api/ponds/{pond}/objects/{name}", auth=cfg, params=params)
        typer.echo(f"Deleted Object '{pond}.{name}'.")
        return
    base = base_table_name(name)  # a companion (changelog, band, droplog, base) deletes its whole table
    try:
        tables = _http.get(f"{cfg['url']}/api/ponds/{pond}/tables", auth=cfg, params=params).json().get("tables", [])
        mode = next((t.get("trickle") for t in tables if t["name"] == base), None)
    except Exception:
        mode = None
    if base != name:
        typer.echo(f"Note: '{name}' is part of table '{base}': deleting it removes the whole table.")
    if mode == "merge":
        typer.echo(f"Note: deleting '{base}' removes its changelog too.")
    elif mode == "append":
        typer.echo(f"Warning: '{base}' is an append Trickle: deleting it drops its accumulated history and droplog.")
    if not yes:
        typer.confirm(f"Delete table '{pond}.{base}' (data and state)?", abort=True)
    _http.delete(f"{cfg['url']}/api/ponds/{pond}/tables/{name}", auth=cfg, params=params)
    typer.echo(f"Deleted '{pond}.{base}'. It returns only if the Pond writes it on a future run.")


@app.command("expose")
def expose(
    ref: str = typer.Argument(..., metavar="POND.TABLE", help="The table, as POND.TABLE."),
    on: bool = typer.Option(False, "--on", help="Expose the table."),
    off: bool = typer.Option(False, "--off", help="Hide the table."),
    default: bool = typer.Option(False, "--default", help="Remove the override and follow pond.toml."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
) -> None:
    """Show or hide a table from read-level keys, overriding pond.toml [serve] tables."""
    from . import _http

    pond, table = split_ref(ref, "a table")
    if sum((on, off, default)) != 1:
        raise typer.BadParameter("pass exactly one of --on, --off or --default")
    exposed = True if on else (False if off else None)
    cfg = _cfg(catchment)
    _http.post(f"{cfg['url']}/api/ponds/{pond}/serve/expose", auth=cfg,
               params=_http.pond_params(major, None), json={"table": table, "exposed": exposed})
    state = "exposed" if on else ("hidden" if off else "back to pond.toml")
    typer.echo(f"{pond}.{table}: {state}.")


def _register_lineage() -> None:
    from . import lineage

    app.command("lineage")(lineage.show)
    app.command("trace")(lineage.trace)


_register_lineage()
