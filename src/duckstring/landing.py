"""Change files: what a Spout in ``mode=changes`` lands, and how to read it back.

Each delivery writes, for every table that changed, one file of the delivery window's changes (or, when the
window can't be read as changes, the whole table), then a **manifest** naming those files. The manifest is the
commit: a file it doesn't name isn't part of the delivery. A reader goes by the manifests, never by listing
the table folders, because a file can be visible before its upload finishes (on a Volume, a new file reads as
empty until then)::

    {destination}/
      orders/2026-11-09T01_00_00+00_00.parquet         the window's changes
      totals/2026-11-09T01_00_00+00_00.full.parquet    the whole table (chunked as .full.{i}.parquet)
      _duckstring/manifests/2026-11-09/01/2026-11-09T01_00_00+00_00.json
      _duckstring/latest.json                          the Spout's own progress (leave it in place)

A change file holds the table's columns plus ``_duckstring_f`` (the delivery's freshness) and either:

- ``_duckstring_op``, for a table with a primary key: one row per changed key, ``+1`` with the key's new row
  (an insert or update) or ``-1`` with its old row (a delete). Deliveries combine by taking each key's row from
  the newest one.
- ``_duckstring_d``, for a table without one: the window as a Z-set, a full row with a weight (``+1`` added,
  ``-1`` removed). Deliveries combine by summing the weights per row.

A full file (``kind: "full"`` in the manifest) is the table as it stands, all of it: a consumer replaces what it
holds. One arrives on a Spout's first delivery, on a resync, and when a Spout fell behind the Source's retained
history. A plain (overwrite) table is republished by every run of its Pond, so it arrives whole every time.

Each delivery has a **position**: its freshness, written as an ISO timestamp, and ``#n`` added for the n-th
delivery again at a freshness already delivered (a resync), whose files take ``.n`` in their names. Positions
order the deliveries; a reader keeps the last one it applied and passes it back as ``after``.

Manifest::

    {"format": 1, "pond": "sales", "major": 1, "delivery": "<position>", "f": "...", "previous_f": "..." | null,
     "tables": {"orders": {"kind": "changes", "pk": ["id"], "column": "_duckstring_op", "rows": 2,
                           "files": ["orders/....parquet"]}}}

:func:`deliveries` lists the committed deliveries after a position, and :func:`changes` reads one table's
changes across them as one DuckDB relation, ready to apply.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

FORMAT = 1
ROOT = "_duckstring"
MANIFESTS = (ROOT, "manifests")
LATEST = (ROOT, "latest.json")
OP = "_duckstring_op"
WEIGHT = "_duckstring_d"
STAMP = "_duckstring_f"


def stem(f: datetime, n: int = 0) -> str:
    """A delivery's file stem: its freshness as a UTC ISO timestamp with ``:`` as ``_`` (sorts in time order),
    and ``.n`` for a repeat delivery at that freshness."""
    text = f.astimezone(timezone.utc).isoformat().replace(":", "_")
    return f"{text}.{n}" if n else text


def manifest_parts(f: datetime, n: int = 0) -> tuple[str, ...]:
    """Where the manifest of the delivery at ``(f, n)`` lives, under the destination (a folder per UTC hour)."""
    f = f.astimezone(timezone.utc)
    return (*MANIFESTS, f.date().isoformat(), f"{f.hour:02d}", f"{stem(f, n)}.json")


def _utc(f: datetime | str | None) -> datetime | None:
    if f is None:
        return None
    f = datetime.fromisoformat(f) if isinstance(f, str) else f
    return f.astimezone(timezone.utc) if f.tzinfo else f.replace(tzinfo=timezone.utc)


def position(value: datetime | str | None) -> tuple[datetime, int] | None:
    """A delivery position ``(f, n)`` from a position string (``ISO[#n]``) or a freshness (``n`` = 0)."""
    if value is None:
        return None
    n = 0
    if isinstance(value, str):
        value, _, rest = value.partition("#")
        n = int(rest) if rest else 0
    return _utc(value), n


def position_text(pos: tuple[datetime, int]) -> str:
    f, n = pos
    text = f.astimezone(timezone.utc).isoformat()
    return f"{text}#{n}" if n else text


def _position_of(name: str) -> tuple[datetime, int]:
    """The position a manifest's file name holds (``{stem}.json``)."""
    base = name.removesuffix(".json")
    head, _, tail = base.rpartition(".")
    if head and tail.isdigit():  # a repeat delivery's ".n" (a fraction of a second always carries "+00_00")
        return _utc(head.replace("_", ":")), int(tail)
    return _utc(base.replace("_", ":")), 0


def deliveries(path: str, *, after: datetime | str | None = None) -> list[dict]:
    """The committed deliveries at ``path`` (a Spout's destination: a local or ``/Volumes`` path, or an
    object-store URI) after the position ``after`` (a position string, or a freshness), oldest first, as their
    manifests. Each file a manifest names is relative to ``path``. Reading stops at a manifest still being
    written, so the list never skips one."""
    from .storage import get_storage

    store = get_storage(path)
    after = position(after)
    since = (after[0].date().isoformat(), f"{after[0].hour:02d}") if after else None
    out: list[dict] = []
    for day in sorted(d for d in store.child(*MANIFESTS).subdir_names() if not since or d >= since[0]):
        for hour in sorted(store.child(*MANIFESTS, day).subdir_names()):
            if since and (day, hour) < since:
                continue
            names = [n for n in store.names(*MANIFESTS, day, hour) if n.endswith(".json") and not n.startswith(".")]
            for name in sorted(names, key=_position_of):
                if after and _position_of(name) <= after:
                    continue
                manifest = _read_manifest(store, (*MANIFESTS, day, hour, name))
                if manifest is None:
                    return out  # still being written: the deliveries after it wait for it
                out.append(manifest)
    return out


def _read_manifest(store, parts) -> dict | None:
    for attempt in (parts, (*parts[:-1], f".{parts[-1]}.duckstring-next")):  # a Volume writer's shadow
        try:
            text = store.read_text(*attempt)
        except FileNotFoundError:
            text = None
        if text:
            try:
                return json.loads(text)
            except ValueError:
                return None
    return None


@dataclass
class Changes:
    """One table's changes across a run of deliveries.

    Attributes:
        relation: A DuckDB relation of the changes. With a primary key: one row per changed key, the table's
            columns plus ``_duckstring_op`` (``+1`` upsert this row, ``-1`` delete this key). Without one: the
            net Z-set, the columns plus ``_duckstring_d``.
        reset: The run includes a full delivery, so ``relation`` holds the whole table (every row ``+1``):
            replace what you hold with it rather than applying it.
        pk: The table's primary key, if it has one.
        f: The freshness of the newest delivery read.
        after: The newest delivery's position: pass it as ``after`` next time.
    """

    relation: object
    reset: bool
    pk: list[str] | None
    f: datetime
    after: str


def changes(con, path: str, table: str, *, after: datetime | str | None = None) -> Changes | None:
    """``table``'s changes in the deliveries at ``path`` after the position ``after``, combined into one relation on
    ``con`` (a DuckDB connection), or None when no delivery since touched it. Deliveries combine as the module
    docstring says; from the newest full delivery on when one is among them."""
    found = [(m, m["tables"][table]) for m in deliveries(path, after=after) if table in m.get("tables", {})]
    if not found:
        return None
    full = max((i for i, (_, t) in enumerate(found) if t["kind"] == "full"), default=None)
    if full is not None:
        found = found[full:]
    pk = found[-1][1].get("pk") or None
    source = _source(con, path, [rel for _, t in found for rel in t["files"]])
    present = set(con.sql(f"SELECT * FROM {source} LIMIT 0").columns)
    if pk:
        keys = ", ".join(_q(c) for c in pk)
        # A full file has no op column (every row is +1): read as NULL where change files share the read.
        op = f"coalesce({_q(OP)}, 1)::TINYINT" if OP in present else "1::TINYINT"
        keep = f"* EXCLUDE ({_q(OP)})" if OP in present else "*"
        inner = f"SELECT {keep}, {op} AS {_q(OP)} FROM {source}"
        rows = (f"SELECT * EXCLUDE (_ds_rank) FROM (SELECT *, row_number() OVER (PARTITION BY {keys} "
                f"ORDER BY {_q(STAMP)} DESC) AS _ds_rank FROM ({inner})) WHERE _ds_rank = 1")
        if full is not None:
            rows = f"SELECT * FROM ({rows}) WHERE {_q(OP)} = 1"
    else:
        weight = f"coalesce({_q(WEIGHT)}, 1)" if WEIGHT in present else "1"
        drop = ", ".join(_q(c) for c in (WEIGHT, STAMP) if c in present)
        rows = (f"SELECT * EXCLUDE ({drop}), sum({weight})::BIGINT AS {_q(WEIGHT)}, max({_q(STAMP)}) AS {_q(STAMP)} "
                f"FROM {source} GROUP BY ALL HAVING sum({weight}) <> 0")
    newest = found[-1][0]
    return Changes(con.sql(rows), full is not None, pk, _utc(newest["f"]), newest["delivery"])


def _source(con, path: str, files: list[str]) -> str:
    """A ``read_parquet`` over the delivery files, columns aligned by name (a later delivery can carry a column
    an earlier one didn't). A Volume DuckDB can't address (the Files API) is fetched into a temporary table."""
    from .storage import get_storage

    store = get_storage(path)
    if store.duckdb_readable:
        store.duckdb_setup(con)
        listed = ", ".join(_lit(store.uri(*f.split("/")).replace("''", "'")) for f in files)
        return f"read_parquet([{listed}], union_by_name = true)"
    local = Path(tempfile.mkdtemp(prefix="duckstring-landing-"))
    try:
        paths = [str(store.fetch(local / f"{i}.parquet", *rel.split("/"))) for i, rel in enumerate(files)]
        name = f"_duckstring_landed_{uuid.uuid4().hex}"
        listed = ", ".join(_lit(p) for p in paths)
        con.execute(f"CREATE TEMP TABLE {name} AS SELECT * FROM read_parquet([{listed}], union_by_name = true)")
        return name
    finally:
        shutil.rmtree(local, ignore_errors=True)


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
