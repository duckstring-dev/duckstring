from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import duckdb
import typer


app = typer.Typer(
    help="Inspect a catchment parquet table.",
    add_completion=False,
    invoke_without_command=True,
)

_VERSION_DIR_RE = re.compile(r"^(?P<pond>[A-Za-z0-9_\-]+)@(?P<maj>\d+)\.(?P<min>\d+)\.(?P<pat>\d+)$")
_SEMVER_DIR_RE = re.compile(r"^(?P<maj>\d+)\.(?P<min>\d+)\.(?P<pat>\d+)$")


@dataclass(frozen=True, order=True)
class SemVer:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse_prefix(cls, s: str) -> tuple[str, tuple[int, ...] | None]:
        """
        Accept:
          - "base" -> ("base", None)
          - "base@0" -> ("base", (0,))
          - "base@0.1" -> ("base", (0,1))
          - "base@0.1.0" -> ("base", (0,1,0))
        """
        s = s.strip()
        if not s:
            raise ValueError("Pond must be non-empty.")

        if "@" not in s:
            return s, None

        pond, ver = s.split("@", 1)
        pond = pond.strip()
        ver = ver.strip()

        if not pond or not ver:
            raise ValueError("Invalid pond ref. Use pond or pond@version (e.g. base@0.1.0).")

        parts = ver.split(".")
        if len(parts) > 3:
            raise ValueError("Version prefix must be major, major.minor, or major.minor.patch (e.g. 0, 0.1, 0.1.0).")

        prefix: list[int] = []
        for p in parts:
            if not p.isdigit():
                raise ValueError("Version prefix must be numeric (e.g. 0, 0.1, 0.1.0).")
            prefix.append(int(p))

        return pond, tuple(prefix)


def _infer_root_dir(repo_root: Path) -> Path:
    """
    Prefer catchment.json's root_dir if present; otherwise default to .duckstring
    """
    spec = repo_root / "catchment.json"
    if spec.exists():
        try:
            data = json.loads(spec.read_text(encoding="utf-8"))
            root = data.get("root_dir") or ".duckstring"
            return (repo_root / root).resolve()
        except Exception:
            pass
    return (repo_root / ".duckstring").resolve()


def _list_version_dirs(data_dir: Path, pond: str) -> list[tuple[SemVer, Path]]:
    out: list[tuple[SemVer, Path]] = []
    if not data_dir.exists():
        return out

    versions: dict[SemVer, Path] = {}

    pond_dir = data_dir / pond
    if pond_dir.exists():
        for p in pond_dir.iterdir():
            if not p.is_dir():
                continue
            m = _SEMVER_DIR_RE.match(p.name)
            if not m:
                continue
            v = SemVer(int(m.group("maj")), int(m.group("min")), int(m.group("pat")))
            versions[v] = p

    for p in data_dir.iterdir():
        if not p.is_dir():
            continue
        m = _VERSION_DIR_RE.match(p.name)
        if not m or m.group("pond") != pond:
            continue
        v = SemVer(int(m.group("maj")), int(m.group("min")), int(m.group("pat")))
        versions.setdefault(v, p)

    out = [(v, d) for v, d in versions.items()]
    out.sort(key=lambda t: t[0])
    return out


def _match_prefix(ver: SemVer, prefix: tuple[int, ...]) -> bool:
    if len(prefix) == 1:
        return ver.major == prefix[0]
    if len(prefix) == 2:
        return ver.major == prefix[0] and ver.minor == prefix[1]
    if len(prefix) == 3:
        return ver.major == prefix[0] and ver.minor == prefix[1] and ver.patch == prefix[2]
    return False


def _resolve_pond_dir(root_dir: Path, pond: str, prefix: tuple[int, ...] | None) -> tuple[Path, str]:
    """
    Returns (pond_dir, display_label_version)

    Resolution rules:
      - pond@X.Y.Z: exact directory must exist
      - pond@X.Y: choose max patch among matching
      - pond@X: choose max minor/patch among matching
      - pond: choose max major/minor/patch overall
    """
    data_dir = root_dir / "data"
    versions = _list_version_dirs(data_dir, pond)

    if prefix is None:
        if versions:
            v, d = versions[-1]
            return d, f"{v.major}.{v.minor}.{v.patch}"
        # fallback for legacy/unversioned layout
        return (data_dir / pond), "<unversioned>"

    # exact: require it exists
    if len(prefix) == 3:
        want = SemVer(prefix[0], prefix[1], prefix[2])
        want_dir = data_dir / pond / f"{want.major}.{want.minor}.{want.patch}"
        if want_dir.exists():
            return want_dir, f"{want.major}.{want.minor}.{want.patch}"
        legacy_dir = data_dir / f"{pond}@{want.major}.{want.minor}.{want.patch}"
        if legacy_dir.exists():
            return legacy_dir, f"{want.major}.{want.minor}.{want.patch}"
        raise FileNotFoundError(f"Missing directory: {want_dir}")

    # prefix: choose max match
    candidates = [(v, d) for (v, d) in versions if _match_prefix(v, prefix)]
    if candidates:
        v, d = candidates[-1]
        return d, f"{v.major}.{v.minor}.{v.patch}"

    # no candidates: report available
    avail = ", ".join(f"{v.major}.{v.minor}.{v.patch}" for (v, _) in versions) or "<none>"
    raise FileNotFoundError(f"No versions found for {pond}@{'.'.join(map(str, prefix))}. Available: {avail}")


def _sql_str(value: str) -> str:
    return value.replace("'", "''")


def _list_tables_with_stats(pond_dir: Path) -> None:
    parquet_files = sorted(pond_dir.glob("*.parquet"))
    if not parquet_files:
        typer.echo("No tables found.")
        return

    con = duckdb.connect(database=":memory:")
    try:
        rows: list[tuple[str, int, int, int]] = []
        for p in parquet_files:
            sql_path = _sql_str(str(p))
            n_rows = con.execute(
                f"SELECT COUNT(*) FROM read_parquet('{sql_path}')"
            ).fetchone()[0]
            schema_rows = con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{sql_path}')"
            ).fetchall()
            n_cols = len(schema_rows)
            rows.append((p.stem, n_rows, n_cols, p.stat().st_size))

        name_w = max(len(r[0]) for r in rows)
        typer.echo("Tables:")
        for name, n_rows, n_cols, size_bytes in rows:
            size_mb = size_bytes / (1024 * 1024)
            typer.echo(
                f"  - {name:<{name_w}}  rows={n_rows:<10} cols={n_cols:<4} size={size_mb:6.2f} MB"
            )
    finally:
        con.close()


def _iter_table_names(pond_dir: Path) -> Iterable[str]:
    if not pond_dir.exists():
        return []
    return sorted(p.stem for p in pond_dir.glob("*.parquet"))


def _iter_pond_names(root_dir: Path) -> Iterable[str]:
    data_dir = root_dir / "data"
    if not data_dir.exists():
        return []

    out: set[str] = set()
    for entry in data_dir.iterdir():
        if not entry.is_dir():
            continue
        m = _VERSION_DIR_RE.match(entry.name)
        if m:
            pond = m.group("pond")
            out.add(pond)
            out.add(f"{pond}@{m.group('maj')}.{m.group('min')}.{m.group('pat')}")
            continue
        if _SEMVER_DIR_RE.match(entry.name):
            continue

        pond = entry.name
        out.add(pond)
        for vdir in entry.iterdir():
            if not vdir.is_dir():
                continue
            m2 = _SEMVER_DIR_RE.match(vdir.name)
            if not m2:
                continue
            out.add(f"{pond}@{m2.group('maj')}.{m2.group('min')}.{m2.group('pat')}")

    return sorted(out)


def _extract_pond_arg(ctx: typer.Context) -> Optional[str]:
    if "pond" in ctx.params and isinstance(ctx.params["pond"], str):
        return ctx.params["pond"]
    return None


def _complete_ponds(ctx: typer.Context, param: typer.CallbackParam, incomplete: str) -> list[str]:
    root_dir = _infer_root_dir(Path.cwd().resolve())
    candidates = _iter_pond_names(root_dir)
    return [c for c in candidates if c.startswith(incomplete)]


def _complete_tables(ctx: typer.Context, param: typer.CallbackParam, incomplete: str) -> list[str]:
    pond_ref = _extract_pond_arg(ctx)
    if not pond_ref:
        return []

    try:
        pond_name, prefix = SemVer.parse_prefix(pond_ref)
        pond_dir, _ = _resolve_pond_dir(_infer_root_dir(Path.cwd().resolve()), pond_name, prefix)
    except Exception:
        return []

    return [t for t in _iter_table_names(pond_dir) if t.startswith(incomplete)]


@app.callback()
def periscope(
    pond: str = typer.Argument(
        ...,
        help="Pond ref: base, base@0, base@0.1, or base@0.1.0",
        shell_complete=_complete_ponds,
    ),
    table: Optional[str] = typer.Argument(
        None,
        help="Table name (omit to list available tables)",
        shell_complete=_complete_tables,
    ),
    limit: int = typer.Option(20, help="Number of rows to show (default: 20)"),
    no_head: bool = typer.Option(False, "--no-head", help="Do not print row preview"),
) -> None:
    """
    Inspect a catchment parquet table: pond[@verprefix] [table]
    """
    repo_root = Path.cwd().resolve()
    root_dir = _infer_root_dir(repo_root)

    try:
        pond_name, prefix = SemVer.parse_prefix(pond)
        table_name = table.strip() if table else None
        if table_name is not None and not table_name:
            raise ValueError("Table must be non-empty.")
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    try:
        pond_dir, resolved_version = _resolve_pond_dir(root_dir, pond_name, prefix)
    except FileNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    if table_name is None:
        label = f"{pond_name}@{resolved_version}" if resolved_version != "<unversioned>" else pond_name
        typer.echo(f"Catchment root: {root_dir}")
        typer.echo(f"Pond: {label}")
        typer.echo(f"Data dir: {pond_dir}")
        _list_tables_with_stats(pond_dir)
        return

    parquet_path = pond_dir / f"{table_name}.parquet"
    if not parquet_path.exists():
        typer.echo(f"Not found: {parquet_path}", err=True)
        if pond_dir.exists():
            available = sorted(p.stem for p in pond_dir.glob("*.parquet"))
            if available:
                typer.echo("Available tables:", err=True)
                for t in available:
                    typer.echo(f"  - {t}", err=True)
        raise typer.Exit(code=1)

    con = duckdb.connect(database=":memory:")
    try:
        rel = con.read_parquet(str(parquet_path))
        n = con.execute("SELECT COUNT(*) FROM rel").fetchone()[0]
        schema_rows = con.execute("DESCRIBE rel").fetchall()

        label = f"{pond_name}@{resolved_version}" if resolved_version != "<unversioned>" else f"{pond_name}.{table_name}"

        typer.echo(f"Catchment root: {root_dir}")
        typer.echo(f"Pond: {label}")
        typer.echo(f"Table: {table_name}")
        typer.echo(f"Parquet: {parquet_path}")
        typer.echo(f"Rows: {n}")
        typer.echo("Schema:")
        for col, typ, *_ in schema_rows:
            typer.echo(f"  - {col}: {typ}")

        if not no_head:
            lim = max(0, int(limit))
            if lim == 0:
                return
            df = con.execute(f"SELECT * FROM rel LIMIT {lim}").df()
            typer.echo(f"\nPreview (first {min(lim, n)} rows):")
            typer.echo(df.to_string(index=False))
    finally:
        con.close()
