"""The writer behind a Spout's ``mode=changes``: one delivery's change files and its manifest (the layout and the
reader are :mod:`duckstring.landing`).

A delivery is exactly-once without a transaction. Before its first data file it records the delivery it is
attempting in ``_duckstring/latest.json`` (``{"f": delivered, "pending": attempting}``, both positions); the
manifest, written
after every data file, is the commit. The next delivery takes the pending one as delivered only if its manifest
is there and whole, so a delivery that died part way is landed again over the same window, under the same
names, and never named by two manifests. Files it wrote that no manifest names are removed on a failure the
worker sees (a process that died leaves them, unnamed and so never read).

A delivery at a freshness already delivered (a resync) takes the next position at it (``#n``, files ``.n``), so it
never replaces a delivery a reader may have applied.

The Catchment is the destination's only writer, so the delivered position is also kept per process, which saves
reading ``latest.json`` back on every delivery.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from .. import landing as L

_FILE_BYTES = 256 * 1024 * 1024  # a delivery's file per table is chunked past this (DuckDB's FILE_SIZE_BYTES)
_KNOWN_LOCK = threading.Lock()
_KNOWN: dict[str, tuple | None] = {}  # destination → the position last delivered there, by this process


def _iso(f: datetime | None) -> str | None:
    return f.astimezone(timezone.utc).isoformat() if f is not None else None


def _text(pos) -> str | None:
    return L.position_text(pos) if pos is not None else None


class Landing:
    """One delivery to a destination ``store`` (a :class:`duckstring.storage.Storage`)."""

    def __init__(self, store, *, key: str, pond: str, major: int, f: datetime):
        self.store, self.key, self.pond, self.major, self.f = store, key, pond, major, f
        self._lock = threading.Lock()
        self._pending_written = False
        self._tables: dict[str, dict] = {}
        self._written: list[tuple[str, ...]] = []
        with _KNOWN_LOCK:
            known = key in _KNOWN
            self._delivered = _KNOWN.get(key)
        if not known:
            self._delivered = self._read_delivered()
        if self._delivered is None or self._delivered[0] < f:
            self.position = (f, 0)
        elif self._delivered[0] == f:
            self.position = (f, self._delivered[1] + 1)  # again at a freshness already delivered (a resync)
        else:
            raise ValueError(f"a delivery at {_iso(f)} is older than the one already landed ({_text(self._delivered)})")

    @property
    def delivered(self) -> datetime | None:
        """The freshness last delivered: the next window starts after it."""
        return self._delivered[0] if self._delivered else None

    def _read_delivered(self) -> tuple | None:
        """The last delivered position from ``latest.json``: its pending delivery when that one's manifest is
        whole (a manifest left empty by a writer that died is removed, so it can't stop a reader)."""
        text = self.store.read_text(*L.LATEST)
        if not text:
            return None
        state = json.loads(text)
        pending = L.position(state.get("pending"))
        if pending is not None:
            parts = L.manifest_parts(*pending)
            if self.store.size(*parts) > 0:
                return pending
            if self.store.exists(*parts):
                self.store.remove(*parts)
        return L.position(state.get("f"))

    # ── per table (called from the worker's threads) ───────────────────────────

    def land_delta(self, con, delta, *, table: str, pk: list[str] | None) -> None:
        """The window's changes of ``table``: one row per changed key with an op when it has a primary key,
        the window's Z-set when it hasn't. Nothing is written for an empty window."""
        if pk:
            names = [c for c in delta.zset.columns if c != L.WEIGHT]
            cols = ", ".join(L._q(c) for c in names)
            match = " AND ".join(f"_ds_gone.{L._q(c)} IS NOT DISTINCT FROM _ds_up.{L._q(c)}" for c in pk)
            # A changed key's new row (+1); a key removed and not re-added, with its old row (-1).
            select = (f"WITH _ds_z AS (SELECT {cols}, sum({L._q(L.WEIGHT)}) AS _ds_w FROM ({delta.zset.sql_query()}) "
                      f"GROUP BY ALL HAVING sum({L._q(L.WEIGHT)}) <> 0) "
                      f"SELECT {cols}, 1::TINYINT AS {L._q(L.OP)} FROM _ds_z WHERE _ds_w > 0 UNION ALL "
                      f"SELECT {', '.join('_ds_gone.' + L._q(c) for c in names)}, -1::TINYINT AS {L._q(L.OP)} "
                      f"FROM (FROM _ds_z WHERE _ds_w < 0) AS _ds_gone ANTI JOIN (FROM _ds_z WHERE _ds_w > 0) AS _ds_up "
                      f"ON {match}")
            column = L.OP
        else:
            select = delta.zset.sql_query()
            column = L.WEIGHT
        self._land(con, select, table=table, kind="changes", pk=pk, column=column)

    def land_full(self, con, relation, *, table: str, pk: list[str] | None) -> None:
        """All of ``table``, written even when it's empty (a consumer replaces what it holds with it)."""
        cols = ", ".join(L._q(c) for c in relation.columns if not c.startswith("_duckstring_"))
        self._land(con, f"SELECT {cols} FROM ({relation.sql_query()})", table=table, kind="full", pk=pk,
                   column=None, keep_empty=True)

    def _land(self, con, select: str, *, table: str, kind: str, pk, column, keep_empty: bool = False) -> None:
        if "/" in table or table.startswith(".") or table == L.ROOT:
            raise ValueError(f"unsafe table name for a change-files path: {table!r}")
        stamped = f"SELECT *, TIMESTAMPTZ '{_iso(self.f)}' AS {L._q(L.STAMP)} FROM ({select})"
        local = tempfile.mkdtemp(prefix="duckstring-landing-")
        try:
            out = os.path.join(local, "rows")
            rows = con.execute(f"COPY ({stamped}) TO {L._lit(out)} (FORMAT PARQUET, FILE_SIZE_BYTES {_FILE_BYTES}, "
                               "OVERWRITE_OR_IGNORE)").fetchall()[0][0]
            if not rows and not keep_empty:
                return
            chunks = sorted(os.listdir(out), key=lambda n: int(n.rsplit("_", 1)[1].split(".")[0]))
            base = L.stem(*self.position) + (".full" if kind == "full" else "")
            names = [f"{base}.parquet"] if len(chunks) == 1 else [f"{base}.{i}.parquet" for i in range(len(chunks))]
            self._mark_pending()
            for chunk, name in zip(chunks, names, strict=True):
                with self._lock:
                    self._written.append((table, name))
                self.store.put_file(Path(out) / chunk, table, name)
            with self._lock:
                self._tables[table] = {"kind": kind, "pk": list(pk) if pk else None, "column": column,
                                       "rows": int(rows), "files": [f"{table}/{n}" for n in names]}
        finally:
            shutil.rmtree(local, ignore_errors=True)

    def _mark_pending(self) -> None:
        with self._lock:
            if self._pending_written:
                return
            self._write_latest({"f": _text(self._delivered), "pending": _text(self.position)})
            self._pending_written = True

    def _write_latest(self, state: dict) -> None:
        self.store.write_text(json.dumps(state), *L.LATEST)

    # ── the delivery ───────────────────────────────────────────────────────────

    def commit(self) -> None:
        """Write the manifest (the commit), or with nothing landed just move the delivered position on, so the
        next window starts here."""
        if self._tables:
            manifest = {"format": L.FORMAT, "pond": self.pond, "major": self.major,
                        "delivery": _text(self.position), "f": _iso(self.f), "previous_f": _iso(self.delivered),
                        "tables": dict(sorted(self._tables.items()))}
            self.store.write_new(json.dumps(manifest, indent=1).encode(), *L.manifest_parts(*self.position))
            done = self.position
        elif self._delivered is None or self._delivered[0] < self.f:
            self._write_latest({"f": _text((self.f, 0))})
            done = (self.f, 0)
        else:
            done = self._delivered
        with _KNOWN_LOCK:
            _KNOWN[self.key] = done

    def abandon(self) -> None:
        """Remove what this attempt wrote (best effort): no manifest names it."""
        with _KNOWN_LOCK:
            _KNOWN.pop(self.key, None)
        for parts in self._written:
            try:
                self.store.remove(*parts)
            except Exception:  # noqa: BLE001 (unnamed by any manifest, so never read)
                pass

