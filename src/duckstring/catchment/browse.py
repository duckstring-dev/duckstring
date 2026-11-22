"""The data viewer's default view of a Trickle table, newest first, at a cost that follows the page, not the table.

An **append** table is its parts, newest Run first.

A **merge** table is shown as its log: first the images written since the base (the ``+1`` rows of its hot change
log, then of its bands, newest file first), then the base. Each row carries ``_duckstring_active``: ``1`` current,
``0`` replaced by a later write, ``-1`` deleted. A page reads only its own rows plus its keys' events above the base,
so it never consolidates the change log: on a 575M-row table with 246M rows above its base, a page took 30 to 175 s
through the reconstruction. The exact current state stays a query away (``SELECT * FROM pond.table``).

A page is placed by each file's row count, from its Parquet footer (or a count of the rows its filter keeps), and
reads only the files it falls in: a LIMIT/OFFSET over the whole read reads every row it skips (23 s for a page 400M
rows into a 1B-row append table)."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..trickle.io import D_COL, F_COL, _key_match, _q, _ts, part_f, part_lo, table_parts

ACTIVE = "_duckstring_active"
_BASE = "_ds_base"  # true for a base row, false for a change image
_ORD = "_ds_ord"  # the file's place in the view
_POS = "_ds_pos"  # the row's place in its file's slice


def _name(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def _name_f(uri: str):
    """A part's freshness from its file name; a tier published unstamped as one file sorts as the oldest."""
    from ..trickle.context import NEVER

    try:
        return part_f(_name(uri))
    except ValueError:
        return NEVER


@dataclass
class _File:
    uri: str
    pred: str | None  # the rows it contributes, or None for all of them
    is_base: bool
    exclude: tuple = ()


@dataclass
class Log:
    """A view over files in a fixed order, paged by position. ``events`` and ``pk`` (a merge table) add each row's
    status; without them the rows are shown as they are."""

    files: list[_File]
    events: str | None = None
    pk: tuple = ()
    _counts: list[int] | None = field(default=None, repr=False)

    def _select(self, f: _File) -> str:
        ex = f" EXCLUDE ({', '.join(_q(c) for c in f.exclude)})" if f.exclude else ""
        where = f" WHERE {f.pred}" if f.pred else ""
        return f"SELECT *{ex} FROM read_parquet(['{f.uri}'], union_by_name=true){where}"

    def counts(self, con) -> list[int]:
        """Each file's row count: its footer's when it has no filter, else a count of the rows the filter keeps."""
        if self._counts is None:
            counts: dict[int, int] = {}
            plain = [i for i, f in enumerate(self.files) if f.pred is None]
            if plain:
                uris = ", ".join(f"'{self.files[i].uri}'" for i in plain)
                rows = dict(con.execute(f"SELECT file_name, num_rows FROM parquet_file_metadata([{uris}])").fetchall())
                for i in plain:
                    counts[i] = int(rows[self.files[i].uri])
            filtered = [i for i, f in enumerate(self.files) if f.pred is not None]
            if filtered:
                union = " UNION ALL ".join(f"SELECT {i} AS i, count(*) AS n FROM ({self._select(self.files[i])})"
                                           for i in filtered)
                counts.update({int(i): int(n) for i, n in con.execute(union).fetchall()})
            self._counts = [counts[i] for i in range(len(self.files))]
        return self._counts

    def count(self, con) -> int:
        return sum(self.counts(con))

    def page(self, con, offset: int, limit: int, order_by: str | None = None, desc: bool = False):
        """One page as a DuckDB result, in view order (or sorted by ``order_by``)."""
        if not self.files:
            return con.execute("SELECT 1 AS _ WHERE false")
        if order_by:
            union = " UNION ALL BY NAME ".join(f"SELECT *, {f.is_base} AS {_BASE} FROM ({self._select(f)})"
                                              for f in self.files)
            order = f"{_q(order_by)} {'DESC' if desc else 'ASC'}, {_BASE}"
            rows = (f"SELECT *, 0 AS {_ORD}, row_number() OVER (ORDER BY {order}) AS {_POS} FROM "
                    f"(SELECT * FROM ({union}) ORDER BY {order} LIMIT {limit} OFFSET {offset})")
        else:
            rows = self._scan_page(con, offset, limit)
        con.execute(f"CREATE OR REPLACE TEMP TABLE _ds_view_page AS {rows}")
        hidden = f"{_BASE}, {_ORD}, {_POS}"
        if not self.pk:
            return con.execute(f"SELECT * EXCLUDE ({hidden}) FROM _ds_view_page ORDER BY {_ORD}, {_POS}")
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
        return con.execute(f"SELECT _p.* EXCLUDE ({hidden}), {status} AS {_q(ACTIVE)} "
                           f"FROM _ds_view_page _p{latest} ORDER BY _p.{_ORD}, _p.{_POS}")

    def _scan_page(self, con, offset: int, limit: int) -> str:
        """The unsorted page: the files it falls in, each read in its own order with its own LIMIT/OFFSET."""
        picks = []
        for i, (f, n) in enumerate(zip(self.files, self.counts(con), strict=True)):
            if offset >= n:
                offset -= n
                continue
            take = min(limit, n - offset)
            picks.append(f"SELECT *, {f.is_base} AS {_BASE}, {i} AS {_ORD}, row_number() OVER () AS {_POS} "
                         f"FROM ({self._select(f)} LIMIT {take} OFFSET {offset})")
            limit -= take
            offset = 0
            if limit <= 0:
                break
        if not picks:
            f = self.files[-1]
            return f"SELECT *, {f.is_base} AS {_BASE}, 0 AS {_ORD}, 0 AS {_POS} FROM ({self._select(f)}) LIMIT 0"
        return " UNION ALL BY NAME ".join(picks)


def append_log(data_dir, table: str, sidecar: dict, f_lo=None, f_hi=None) -> Log | None:
    """An append table's snapshot (the files :meth:`DataPlane.read_select` reads) newest first, within the freshness
    window ``[f_lo, f_hi]`` when given (a file wholly outside it is left out). A file reaching past the snapshot's
    freshness, below the table's floor or across the window keeps only the rows in it. ``None`` when the table
    isn't a parts directory."""
    from ..dataplane import _as_storage, _parts_floor, _run_upper, parts_snapshot

    store = _as_storage(data_dir)
    if not table_parts(store, table):
        return None
    names = parts_snapshot(store, table, sidecar)
    if not names:
        return None
    upper = _run_upper(sidecar, None)
    floor = _parts_floor(sidecar, table, upper)
    files = []
    for n in sorted(names, key=part_f, reverse=True):
        lo, hi = part_lo(n), part_f(n)
        if (f_lo is not None and hi < f_lo) or (f_hi is not None and lo > f_hi):
            continue
        preds = []
        if upper is not None and hi > upper:
            preds.append(f"{_q(F_COL)} <= {_ts(upper)}")
        if floor is not None and lo < floor:
            preds.append(f"{_q(F_COL)} >= {_ts(floor)}")
        if f_lo is not None and lo < f_lo:
            preds.append(f"{_q(F_COL)} >= {_ts(f_lo)}")
        if f_hi is not None and hi > f_hi:
            preds.append(f"{_q(F_COL)} <= {_ts(f_hi)}")
        files.append(_File(store.uri(table, n), " AND ".join(preds) or None, True))
    return Log(files)


def merge_log(data_dir, table: str, sidecar: dict, pk) -> Log:
    """The log view of merge main ``table`` from its read snapshot (:func:`~duckstring.dataplane.merge_snapshot`)."""
    from ..dataplane import _as_storage, _clog_select, merge_snapshot

    store = _as_storage(data_dir)
    snap = merge_snapshot(store, table, sidecar)
    above = f" AND {_q(F_COL)} > {_ts(snap['f_base'])}" if snap["f_base"] is not None else ""
    files: list[_File] = []
    hot_after = snap.get("hot_after")
    # Newest first: the hot parts by freshness, then the bands. A hot union reaching below the newest band
    # repeats what the band holds; its rows at or below it are left out, as every read of the snapshot does.
    for uris, after in ((snap["hot"], hot_after), (snap["bands"], None)):
        lo = f" AND {_q(F_COL)} > {_ts(after)}" if after is not None else ""
        for uri in sorted(uris, key=_name_f, reverse=True):
            files.append(_File(uri, f"{_q(D_COL)} > 0{above}{lo}", False, (D_COL,)))
    events = None
    if files:
        events = _clog_select(snap)
        if above:
            events = f"SELECT * FROM ({events}) WHERE true{above}"
    files += [_File(uri, None, True) for uri in snap["base"]]
    return Log(files, events, tuple(pk))
