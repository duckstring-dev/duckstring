"""Sampled verification of a SQL engine's result (plans/flock-coverage.md, part 4).

The allow-list says which constructs an engine was measured to agree on; verification checks the actual
result. After a dispatch to an engine that isn't DuckDB, and after ``conform``:

1. Trace each key column to the source column it passes through from verbatim, and take a random sample of
   that column's values from the source (``DUCKSTRING_FLOCK_VERIFY_KEYS``, default 1,000). Sampling the
   source rather than the result is what lets a row the engine dropped be caught.
2. Recompute only those keys locally: the source is restricted to the sampled values before the job runs (the
   builder works row by row on a source's rows, so the restricted job yields exactly the sampled keys' rows).
3. Compare with the engine's rows for the same keys in both directions, so missing, extra and wrong rows are
   all caught. Floating-point columns are
   compared within a relative 1e-9 when the Pond accepts ``float_order``, else exactly.

A mismatch rejects the result: the job runs locally and the dispatch counts as failed, with the number of
disagreeing keys in its error. A sample catches a systematic difference (wrong on every row) and can miss a
rare one (a value exactly halfway); it complements the allow-list rather than replacing it. When a key can't be
traced (it is computed, or the job is a ``.sql()`` query), verification is skipped and logged.
"""

from __future__ import annotations

import logging
import math
import uuid

log = logging.getLogger("duckstring.flock.verify")

DEFAULT_KEYS = 1_000
_REL_TOL = 1e-9


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def sample_size(env) -> int:
    raw = (env or {}).get("DUCKSTRING_FLOCK_VERIFY_KEYS")
    try:
        return max(0, int(raw)) if raw not in (None, "") else DEFAULT_KEYS
    except ValueError:
        return DEFAULT_KEYS


def key_columns(job) -> tuple[str, ...]:
    if job.kind == "aggregate":
        return tuple(job.builder._agg["by"])
    return tuple(job.out_pk)


def verify(job, result, *, keys: int, accept=()) -> str | None:
    """``None`` when the sample agrees (or verification can't apply and is skipped); else why it doesn't."""
    if keys <= 0:
        return None
    cols = key_columns(job)
    builder = job.builder
    sources = [builder._key_source(c) for c in cols] if cols else [None]
    if not cols or any(s is None for s in sources) or len({id(s[0]) for s in sources}) != 1:
        log.info("flock: result not verified (the key %s can't be traced to one source column)", list(cols))
        return None
    leaf = sources[0][0]
    leaf_cols = [s[1] for s in sources]
    con = job.ctx.con
    tag = uuid.uuid4().hex[:8]
    out, sample, src = f"_ds_verify_out_{tag}", f"_ds_verify_keys_{tag}", f"_ds_verify_leaf_{tag}"
    result.create_view(out, replace=True)
    key_list = ", ".join(_q(c) for c in cols)
    job.ctx.read_table(leaf.ref).create_view(src, replace=True)
    picked = ", ".join(f"{_q(lc)} AS {_q(c)}" for lc, c in zip(leaf_cols, cols, strict=True))
    con.execute(f"CREATE TEMP TABLE {_q(sample)} AS SELECT DISTINCT * FROM (SELECT {picked} FROM {_q(src)} "
                f"WHERE {' AND '.join(f'{_q(c)} IS NOT NULL' for c in leaf_cols)} USING SAMPLE {int(keys)} ROWS)")
    try:
        views = {}
        for i, lf in enumerate(job.leaves()):
            name = f"_ds_verify_src_{tag}_{i}"
            job.ctx.read_table(lf.ref).create_view(name, replace=True)
            views[id(lf)] = name

        def read(lf) -> str:
            sel = f"SELECT {', '.join(_q(c) for c in job.columns(lf))} FROM {_q(views[id(lf)])}"
            if lf is leaf:
                sel += (f" WHERE ({', '.join(_q(c) for c in leaf_cols)}) IN "
                        f"(SELECT {key_list} FROM {_q(sample)})")
            return sel

        names = list(job.schema())
        proj = ", ".join(_q(c) for c in names)
        local = con.execute(f"SELECT {proj} FROM ({job.sql(read)}) AS {_q('_ds_local')}").fetchall()
        remote = con.execute(f"SELECT {proj} FROM {_q(out)} WHERE ({key_list}) IN "
                             f"(SELECT {key_list} FROM {_q(sample)})").fetchall()
    finally:
        con.execute(f"DROP TABLE IF EXISTS {_q(sample)}")
    idx = [names.index(c) for c in cols]
    tolerant = "float_order" in accept
    by_key = {}
    for row in local:
        by_key.setdefault(tuple(row[i] for i in idx), [[], []])[0].append(row)
    for row in remote:
        by_key.setdefault(tuple(row[i] for i in idx), [[], []])[1].append(row)
    bad = [k for k, (lr, rr) in by_key.items() if not _same(lr, rr, tolerant)]
    if bad:
        return (f"sampled verification: {len(bad)} of {len(by_key)} sampled keys differ from DuckDB "
                f"(first: {bad[0]!r})")
    return None


def _same(local: list, remote: list, tolerant: bool) -> bool:
    if len(local) != len(remote):
        return False
    return all(_row_eq(a, b, tolerant) for a, b in zip(sorted(local, key=repr), sorted(remote, key=repr), strict=True))


def _row_eq(a, b, tolerant: bool) -> bool:
    for x, y in zip(a, b, strict=True):
        if isinstance(x, float) and isinstance(y, float):
            if math.isnan(x) and math.isnan(y):
                continue
            if x == y or (tolerant and math.isclose(x, y, rel_tol=_REL_TOL, abs_tol=1e-300)):
                continue
            return False
        if x != y:
            return False
    return True
