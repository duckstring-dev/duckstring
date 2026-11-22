"""The data viewer's default view of a Trickle table, newest first, at a cost that follows the page, not the table.

An **append** table is its parts, newest Run first.

A **merge** table is shown as its log: first the images written since the base (the ``+1`` rows of its hot change
log, then of its bands, newest file first), then the base. Each row carries ``_duckstring_active``: ``1`` current,
``0`` replaced by a later write, ``-1`` deleted. A page reads only its own rows plus its keys' events above the base,
so it never consolidates the change log: on a 575M-row table with 246M rows above its base, a page took 30 to 175 s
through the reconstruction. The count is the regions' row counts. The exact current state stays a query away
(``SELECT * FROM pond.table``)."""

from __future__ import annotations

from dataclasses import dataclass

from ..trickle.io import D_COL, F_COL, _key_match, _q, _ts, first_row, part_f, table_parts

ACTIVE = "_duckstring_active"
_BASE = "_ds_base"  # true for a base row, false for a change image
_REGION = "_ds_region"
_POS = "_ds_pos"


def _name_f(uri: str):
    """A part's freshness from its file name; a tier published unstamped as one file sorts as the oldest."""
    from ..trickle.context import NEVER

    try:
        return part_f(uri.rsplit("/", 1)[-1])
    except ValueError:
        return NEVER


def append_newest_first(data_dir, table: str, sidecar: dict) -> str | None:
    """An append table's snapshot (as :meth:`DataPlane.read_select` reads it) with its files newest first, so a
    scan-ordered page starts at the latest Run. ``None`` when it isn't a parts directory."""
    from ..dataplane import _as_storage, _parts_since, _read_parquet_files, _run_upper, parts_snapshot

    store = _as_storage(data_dir)
    if not table_parts(store, table):
        return None
    names = parts_snapshot(store, table, sidecar)
    if not names:
        return None
    names = sorted(names, key=part_f, reverse=True)
    return _read_parquet_files([store.uri(table, n) for n in names], _run_upper(sidecar, None),
                               since=_parts_since(sidecar, table, names))


@dataclass
class MergeLog:
    regions: list[tuple[str, bool]]  # (SELECT, is_base) in view order: change images, then the base
    events: str | None  # every change log event above the base (for the page's status lookup)
    pk: tuple

    def count_sql(self) -> str:
        parts = [f"(SELECT count(*) FROM ({s}))" for s, _ in self.regions]
        return "SELECT " + (" + ".join(parts) if parts else "0")

    def page(self, con, offset: int, limit: int, order_by: str | None = None, desc: bool = False):
        """One page as a DuckDB result: the rows in view order, each with ``_duckstring_active``."""
        if not self.regions:
            return con.execute("SELECT 1 AS _ WHERE false")
        if order_by:
            union = " UNION ALL BY NAME ".join(f"SELECT *, {b} AS {_BASE} FROM ({s})" for s, b in self.regions)
            order = f"{_q(order_by)} {'DESC' if desc else 'ASC'}, {_BASE}"
            rows = (f"SELECT *, 0 AS {_REGION}, row_number() OVER (ORDER BY {order}) AS {_POS} FROM "
                    f"(SELECT * FROM ({union}) ORDER BY {order} LIMIT {limit} OFFSET {offset})")
        else:
            rows = self._scan_page(con, offset, limit)
        con.execute(f"CREATE OR REPLACE TEMP TABLE _ds_view_page AS {rows}")
        if self.events is None:
            status, latest = "1", ""
        else:
            keys = ", ".join(_q(c) for c in self.pk)
            con.execute(
                f"CREATE OR REPLACE TEMP TABLE _ds_view_latest AS SELECT {keys}, "
                f"max(struct_pack(f := {_q(F_COL)}, d := {_q(D_COL)})) AS _ds_last FROM ({self.events}) _e "
                f"SEMI JOIN _ds_view_page _p ON {_key_match('_e', '_p', self.pk)} GROUP BY ALL")
            latest = f" LEFT JOIN _ds_view_latest _l ON {_key_match('_p', '_l', self.pk)}"
            # A change image is current when it is its key's newest event; a base row when its key has none.
            status = (f"CASE WHEN _l._ds_last IS NULL THEN 1 WHEN _l._ds_last.d < 0 THEN -1 "
                      f"WHEN NOT _p.{_BASE} AND _l._ds_last.f = _p.{_q(F_COL)} THEN 1 ELSE 0 END")
        return con.execute(f"SELECT _p.* EXCLUDE ({_BASE}, {_REGION}, {_POS}), {status} AS {_q(ACTIVE)} "
                           f"FROM _ds_view_page _p{latest} ORDER BY _p.{_REGION}, _p.{_POS}")

    def _scan_page(self, con, offset: int, limit: int) -> str:
        """The unsorted page: each region read in file order with its own LIMIT/OFFSET (a UNION's order isn't
        promised), the regions' counts placing the page."""
        picks = []
        for i, (sql, is_base) in enumerate(self.regions):
            last = i == len(self.regions) - 1
            n = None if last else first_row(con.execute(f"SELECT count(*) FROM ({sql})"))[0]
            if n is not None and offset >= n:
                offset -= n
                continue
            take = limit if n is None else min(limit, n - offset)
            picks.append(f"SELECT *, {is_base} AS {_BASE}, {i} AS {_REGION}, row_number() OVER () AS {_POS} "
                         f"FROM ({sql} LIMIT {take} OFFSET {offset})")
            limit -= take
            offset = 0
            if limit <= 0:
                break
        if not picks:
            sql, is_base = self.regions[-1]
            return (f"SELECT *, {is_base} AS {_BASE}, 0 AS {_REGION}, 0 AS {_POS} FROM ({sql}) LIMIT 0")
        return " UNION ALL BY NAME ".join(picks)


def merge_log(data_dir, table: str, sidecar: dict, pk) -> MergeLog:
    """The log view of merge main ``table`` from its read snapshot (:func:`~duckstring.dataplane.merge_snapshot`)."""
    from ..dataplane import _as_storage, _clog_select, _read_parquet_files, merge_snapshot

    store = _as_storage(data_dir)
    snap = merge_snapshot(store, table, sidecar)
    above = f" AND {_q(F_COL)} > {_ts(snap['f_base'])}" if snap["f_base"] is not None else ""
    regions: list[tuple[str, bool]] = []
    hot_after = snap.get("hot_after")
    # Newest first: the hot parts by freshness, then the bands. A hot union reaching below the newest band
    # repeats what the band holds; its rows at or below it are left out, as every read of the snapshot does.
    for uris, after in ((snap["hot"], hot_after), (snap["bands"], None)):
        if not uris:
            continue
        lo = f" AND {_q(F_COL)} > {_ts(after)}" if after is not None else ""
        files = sorted(uris, key=_name_f, reverse=True)
        regions.append((f"SELECT * EXCLUDE ({_q(D_COL)}) FROM ({_read_parquet_files(files)}) "
                        f"WHERE {_q(D_COL)} > 0{above}{lo}", False))
    events = None
    if regions:
        events = _clog_select(snap)
        if above:
            events = f"SELECT * FROM ({events}) WHERE true{above}"
    if snap["base"]:
        regions.append((_read_parquet_files(snap["base"]), True))
    return MergeLog(regions, events, tuple(pk))
