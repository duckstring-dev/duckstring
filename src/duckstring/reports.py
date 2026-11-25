"""Run reports: facts a Ripple declares about its Run, carried downstream with the data.

``pond.report({...})`` stages a JSON dict for the Run. At export the Run's reports, and every report its Sources
carried in the Run's window, are appended to a hidden append table, :data:`REPORTS_TABLE`, keyed by
``(pond, run_f)``: ``pond`` is the reporting line's ``name@major``, ``run_f`` its Run's freshness. A downstream
Run reads its Sources' tables over its own window, so it holds a row for every upstream Run whose data it read,
including the ones a rarely run Pond catches up on. ``pond.reports()`` reads them back.

It is an ordinary append Trickle, so windows, coverage, Draws, registry recovery and read snapshots apply as to
any other. It is hidden from table listings and the schema contract. A pipeline that never reports writes nothing.
Nothing in orchestration reads it. See plans/showcase-fixes.md item 14.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from .trickle.context import SYSTEM_PREFIX

REPORTS_TABLE = f"{SYSTEM_PREFIX}reports"
# Registry-only (never published: the reserved prefix keeps it out of the publish set): each Ripple's reports for
# a Run, ``(f, ripple, reports)``, until the Run's export collects them. In the registry rather than in memory, so
# a Duck that restarts after a Ripple completed still has its report.
STAGED_TABLE = f"{SYSTEM_PREFIX}report_staged"
PK = ["pond", "run_f"]


def _exists(con, table: str) -> bool:
    return bool(con.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE table_name = ? AND schema_name = 'main' "
        "AND database_name = current_database()", [table]).fetchall()[0][0])


def _ensure_staged(con) -> None:
    if _exists(con, STAGED_TABLE):
        return
    from .core import retry_on_lock

    # Ripples run concurrently on cursors of one instance: two creating it at once conflict, and one retries.
    retry_on_lock(lambda: con.execute(
        f"CREATE TABLE IF NOT EXISTS {STAGED_TABLE} (f TIMESTAMPTZ, ripple VARCHAR, reports VARCHAR)"))


def _default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} isn't JSON-serialisable")


def encode(reports: dict) -> str:
    """``reports`` as canonical JSON (sorted keys, so two copies of one report are one row). Datetimes and
    dates become ISO strings; anything else JSON can't hold raises ``TypeError``."""
    if not isinstance(reports, dict):
        raise TypeError(f"pond.report() takes a dict, not {type(reports).__name__}")
    for key in reports:
        if not isinstance(key, str):
            raise TypeError(f"pond.report() keys are strings, not {type(key).__name__} ({key!r})")
    try:
        return json.dumps(reports, sort_keys=True, default=_default)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"pond.report(): {exc}") from None


def stage(con, f, ripple: str | None, reports: dict) -> None:
    """Merge ``reports`` into what ``ripple`` has reported for the Run at ``f`` (later keys win)."""
    encode(reports)  # validate before touching the registry
    _ensure_staged(con)
    ripple = ripple or ""
    rows = con.execute(f"SELECT reports FROM {STAGED_TABLE} WHERE f = ? AND ripple = ?", [f, ripple]).fetchall()
    merged = json.loads(rows[0][0]) if rows else {}
    merged.update(reports)
    from .core import retry_on_lock

    def _write() -> None:
        con.execute("BEGIN")
        try:
            con.execute(f"DELETE FROM {STAGED_TABLE} WHERE f = ? AND ripple = ?", [f, ripple])
            con.execute(f"INSERT INTO {STAGED_TABLE} VALUES (?, ?, ?)", [f, ripple, encode(merged)])
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

    retry_on_lock(_write)


def clear(con, f, ripple: str | None) -> None:
    """Forget what ``ripple`` reported for the Run at ``f``: a Ripple launching again (a retry, a replay)
    reports afresh."""
    if f is None or not _exists(con, STAGED_TABLE):
        return
    from .core import retry_on_lock

    retry_on_lock(lambda: con.execute(f"DELETE FROM {STAGED_TABLE} WHERE f = ? AND ripple = ?", [f, ripple or ""]))


def staged(con, f) -> dict:
    """The Run's own reports: every Ripple's, merged in Ripple-name order."""
    if f is None or not _exists(con, STAGED_TABLE):
        return {}
    out: dict = {}
    for (reports,) in con.execute(f"SELECT reports FROM {STAGED_TABLE} WHERE f = ? ORDER BY ripple", [f]).fetchall():
        out.update(json.loads(reports))
    return out


def _sources(pond) -> list[str]:
    declared = pond._declared_sources
    names = (set(declared) if declared is not None
             else set(pond.source_majors) | set(pond.source_dirs) | set(pond.remote_sources))
    return sorted(names)


def source_reports(pond, source: str, *, window: bool):
    """A Source's reports as a relation ``(pond, run_f, reports)``: those in the Run's window, or (without one,
    an App) everything the Source holds. ``None`` when the Source has published none."""
    from . import trickle_io as trickle
    from .core import MissingSourceAsset
    from .dataplane import get_data_plane

    remote = pond.remote_sources.get(source)
    if remote is not None:  # a live Puddle: through its Catchment's query API
        if not remote.has_reports():
            return None
        if window:
            delta = remote.delta(pond.con, REPORTS_TABLE, pond._previous_position(source), pond._position(source))
            return delta.zset.filter("_duckstring_d > 0").select("pond, run_f, reports")
        return remote.relation(pond.con, REPORTS_TABLE, pond._position(source)).select("pond, run_f, reports")
    try:
        data_dir = pond._source_data_dir(source)
        if REPORTS_TABLE not in trickle.sidecar_tables(trickle.load_sidecar(data_dir)):
            return None
    except (FileNotFoundError, MissingSourceAsset):
        return None
    con = pond.con
    data_dir.duckdb_setup(con)
    dp = get_data_plane()
    pin = pond._read_position(source)
    if window:  # the Source's changes since the previous Run read it (plans/source-read-positions.md)
        delta = trickle.read_delta(con, data_dir, REPORTS_TABLE, pond._previous_position(source),
                                   pond._position(source), dp=dp, pin=pin, materialise=True)
        rel = delta.zset.filter("_duckstring_d > 0")
    else:
        rel = con.sql(dp.read_select(data_dir, REPORTS_TABLE, as_of=pond._position(source), pin=pin))
    return rel.select("pond, run_f, reports")


def read(pond) -> dict:
    """``{pond: {run_f: reports}}`` from every Source (see :meth:`duckstring.core.Pond.reports`)."""
    window = pond.f is not None
    out: dict = {}
    for source in _sources(pond):
        rel = source_reports(pond, source, window=window)
        if rel is None:
            continue
        for key, run_f, reports in rel.order("pond, run_f").fetchall():
            out.setdefault(key, {})[run_f.astimezone(timezone.utc)] = json.loads(reports)
    return out


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def own_key(pond) -> str:
    from .keys import pond_key, spec_major

    return pond_key(pond.name, spec_major(pond.version))


def collect(pond) -> bool:
    """Append the Run's own reports and its Sources' reports in its window to :data:`REPORTS_TABLE`, through
    ``pond`` (a handle at the Run's ``f``/``previous_f``). Run at export, after every Ripple. Writes nothing
    when there is nothing to carry. Returns whether rows were appended."""
    from . import trickle_io as trickle

    con = pond.con
    rels = []
    own = staged(con, pond.f)
    if own:
        # Literals, not parameters: the relation is named as a view, and a view can't carry parameters.
        rels.append(con.sql(
            f"SELECT {_literal(own_key(pond))}::VARCHAR AS pond, "
            f"{_literal(pond.f.astimezone(timezone.utc).isoformat())}::TIMESTAMPTZ AS run_f, "
            f"{_literal(encode(own))}::VARCHAR AS reports"))
    for source in _sources(pond):
        rel = source_reports(pond, source, window=True)
        if rel is not None:
            rels.append(rel)
    changed = False
    if rels:
        union = rels[0]
        for rel in rels[1:]:
            union = union.union(rel)
        if union.aggregate("count(*)").fetchall()[0][0]:
            # keep_prior: an upstream Run reached through two Sources, or read again by an overlapping window,
            # is one row. Identical copies collapse; a row is never rewritten.
            changed = trickle.append_table(con, REPORTS_TABLE, union, pond.f, PK, on_conflict="keep_prior",
                                           log_drops=False)
    if _exists(con, STAGED_TABLE):
        # Earlier Runs' only: a replay of this Run (its export failed, or the Duck restarted before it finished)
        # collects its own reports again.
        con.execute(f"DELETE FROM {STAGED_TABLE} WHERE f < ?", [pond.f])
    return changed
