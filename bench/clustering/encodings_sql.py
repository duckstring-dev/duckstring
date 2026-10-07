"""SQL for the row orders the clustering benchmark compares.

Every interleaved order is two steps over the clustering columns:

1. **Bucket** each column into ``2**b`` integers. *Scaled* maps the value linearly between the column's min and
   max (what a plain Morton or Hilbert implementation does). *Ranked* uses the value's quantile rank, which
   flattens the distribution to uniform.
2. **Curve**: interleave the buckets' bits (Morton / Z-order) or map them onto a Hilbert curve.

So ``morton`` = scaled + Morton, ``rank_morton`` = ranked + Morton, and likewise for Hilbert. Three ways of ranking:

- ``rank_*`` use ``NTILE`` windows, as Duckstring 0.6.0 did. Each window carries every column through a sort.
- ``qrank_*`` take each column's bucket boundaries from one ``quantile_disc`` aggregate, and a row's bucket is
  the number of boundaries its value exceeds. That sorts one column at a time instead of the full-width rows,
  and puts equal values in the same bucket, where ``NTILE`` may split them.
- ``arank_*`` take the boundaries from ``approx_quantile`` (a streaming t-digest, one parallel pass, no sort)
  and find a row's bucket by binary search (``log2(buckets)`` comparisons instead of one per boundary).
  ``arank_hilbert`` is what Duckstring's ``cluster_by`` now does.

In every bucketing a column that has NULLs keeps its top bucket for them, as Duckstring does, so the
transforms differ only in how non-NULL values are spread.
"""

from __future__ import annotations

LAYOUTS = ("generated", "hash", "lexicographic", "morton", "hilbert", "rank_morton", "rank_hilbert",
           "qrank_morton", "qrank_hilbert", "arank_morton", "arank_hilbert")
INTERLEAVED = ("morton", "hilbert", "rank_morton", "rank_hilbert", "qrank_morton", "qrank_hilbert",
               "arank_morton", "arank_hilbert")


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def bucket_cols(n: int) -> list[str]:
    return [f"_bk{i}" for i in range(n)]


def ranked_sql(source: str, cols: list[str], b: int, nullable: list[bool]) -> str:
    """``source`` plus ``_bk{i}``: each column's quantile rank in ``2**b`` equal-population buckets. The same
    expressions as Duckstring 0.6.0's ``rank_morton_sql``."""
    exprs = []
    for i, (c, has_null) in enumerate(zip(cols, nullable, strict=True)):
        if has_null:
            top = (1 << b) - 1
            exprs.append(f"CASE WHEN {q(c)} IS NULL THEN {top} ELSE NTILE({max(top, 1)}) OVER "
                         f"(PARTITION BY {q(c)} IS NULL ORDER BY {q(c)}) - 1 END AS _bk{i}")
        else:
            exprs.append(f"NTILE({1 << b}) OVER (ORDER BY {q(c)}) - 1 AS _bk{i}")
    return f"SELECT *, {', '.join(exprs)} FROM {source}"


def quantile_bounds_sql(source: str, col: str, b: int, has_null: bool, approx: bool = False) -> str:
    """The query for ``col``'s bucket boundaries: the values at the ``k / buckets`` quantiles, where
    ``buckets`` is ``2**b`` (one fewer if the column has NULLs, which keep the top bucket). Exact
    (``quantile_disc``, a sort of the column) or ``approx`` (``approx_quantile``, a t-digest)."""
    buckets = max((1 << b) - 1, 1) if has_null else 1 << b
    cast = "::FLOAT" if approx else ""  # approx_quantile only binds a FLOAT[] list
    fractions = ", ".join(f"({k}/{buckets}){cast}" for k in range(1, buckets))
    fn = "approx_quantile" if approx else "quantile_disc"
    return f"SELECT {fn}({q(col)}, [{fractions}]) FROM {source}" if buckets > 1 else "SELECT []"


def _bisect_expr(col: str, bounds: list, lo: int, hi: int) -> str:
    """The number of ``bounds[lo:hi]`` that ``col`` exceeds, plus ``lo``, by binary search (bounds sorted)."""
    if lo == hi:
        return str(lo)
    mid = (lo + hi) // 2
    return (f"CASE WHEN {q(col)} > {bounds[mid]} THEN {_bisect_expr(col, bounds, mid + 1, hi)} "
            f"ELSE {_bisect_expr(col, bounds, lo, mid)} END")


def qranked_sql(source: str, cols: list[str], b: int, stats: list[dict], bisect: bool = False) -> str:
    """``source`` plus ``_bk{i}``: each column's quantile bucket from precomputed boundaries
    (``stats[i]["bounds"]``, from :func:`quantile_bounds_sql`): a sum of one comparison per boundary, or with
    ``bisect`` a binary search."""
    exprs = []
    for i, (c, s) in enumerate(zip(cols, stats, strict=True)):
        top = (1 << b) - 1
        bounds = sorted(s["bounds"] or [])
        if bisect:
            count = _bisect_expr(c, bounds, 0, len(bounds))
        else:
            count = " + ".join(f"({q(c)} > {v})::BIGINT" for v in bounds) or "0"
        null_bucket = top if s["nulls"] > 0 else 0
        exprs.append(f"CASE WHEN {q(c)} IS NULL THEN {null_bucket} ELSE {count} END AS _bk{i}")
    return f"SELECT *, {', '.join(exprs)} FROM {source}"


def scaled_sql(source: str, cols: list[str], b: int, stats: list[dict]) -> str:
    """``source`` plus ``_bk{i}``: each column scaled linearly from ``[min, max]`` onto its buckets. ``stats[i]``
    holds the column's ``min``, ``max`` and ``nulls``."""
    exprs = []
    for i, (c, s) in enumerate(zip(cols, stats, strict=True)):
        has_null = s["nulls"] > 0
        top = (1 << b) - 1
        buckets = max(top, 1) if has_null else 1 << b
        lo, span = float(s["min"]), float(s["max"]) - float(s["min"])
        scaled = ("0" if span <= 0 else
                  f"LEAST({buckets - 1}, CAST(floor(({q(c)}::DOUBLE - {lo!r}) * {buckets} / {span!r}) AS BIGINT))")
        null_bucket = top if has_null else 0
        exprs.append(f"CASE WHEN {q(c)} IS NULL THEN {null_bucket} ELSE {scaled} END AS _bk{i}")
    return f"SELECT *, {', '.join(exprs)} FROM {source}"


def interleave_expr(n: int, b: int) -> str:
    """The Morton key of ``_bk0.._bk{n-1}`` (``b`` bits each): one bit per column per round, most significant
    first, ``_bk0`` leading each round."""
    terms, p = [], n * b - 1
    for depth in range(b):
        for i in range(n):
            terms.append(f"(((_bk{i} >> {b - 1 - depth}) & 1) << {p})")
            p -= 1
    return f"CAST({' + '.join(terms)} AS BIGINT)"


def _layer(sql: str, assigns: dict[str, str]) -> str:
    rep = ", ".join(f"{e} AS {c}" for c, e in assigns.items())
    return f"SELECT * REPLACE ({rep}) FROM ({sql})"


def hilbert_transpose_sql(sql: str, n: int, b: int) -> str:
    """Rewrite ``_bk0.._bk{n-1}`` (``b`` bits each) in place into the *transposed* Hilbert index, unrolling
    Skilling's ``AxestoTranspose`` ("Programming the Hilbert curve", 2004) into one projection per step. Each
    step reads the previous step's values, matching the algorithm's sequential updates. Interleaving the
    result (``interleave_expr``) gives the Hilbert index."""
    big_q = 1 << (b - 1)
    qq = big_q
    while qq > 1:  # inverse undo
        p = qq - 1
        sql = _layer(sql, {"_bk0": f"CASE WHEN (_bk0 & {qq}) <> 0 THEN xor(_bk0, {p}) ELSE _bk0 END"})
        for i in range(1, n):
            t = f"(xor(_bk0, _bk{i}) & {p})"
            sql = _layer(sql, {
                "_bk0": f"CASE WHEN (_bk{i} & {qq}) <> 0 THEN xor(_bk0, {p}) ELSE xor(_bk0, {t}) END",
                f"_bk{i}": f"CASE WHEN (_bk{i} & {qq}) <> 0 THEN _bk{i} ELSE xor(_bk{i}, {t}) END",
            })
        qq >>= 1
    for i in range(1, n):  # Gray encode
        sql = _layer(sql, {f"_bk{i}": f"xor(_bk{i}, _bk{i - 1})"})
    terms, qq = [], big_q
    while qq > 1:
        terms.append(f"CASE WHEN (_bk{n - 1} & {qq}) <> 0 THEN {qq - 1} ELSE 0 END")
        qq >>= 1
    if terms:
        t = terms[0]
        for term in terms[1:]:
            t = f"xor({t}, {term})"
        sql = _layer(sql, {f"_bk{i}": f"xor(_bk{i}, {t})" for i in range(n)})
    return sql


def keyed_sql(layout: str, source: str, cols: list[str], b: int, stats: list[dict]) -> str:
    """``source`` with a ``_key`` column to sort by, for an interleaved ``layout``."""
    n = len(cols)
    if layout.startswith("rank_"):
        sql = ranked_sql(source, cols, b, [s["nulls"] > 0 for s in stats])
    elif layout.startswith("qrank_"):
        sql = qranked_sql(source, cols, b, stats)
    elif layout.startswith("arank_"):
        sql = qranked_sql(source, cols, b, stats, bisect=True)
    else:
        sql = scaled_sql(source, cols, b, stats)
    if layout.endswith("hilbert"):
        sql = hilbert_transpose_sql(sql, n, b)
    excl = ", ".join(bucket_cols(n))
    return f"SELECT * EXCLUDE ({excl}), {interleave_expr(n, b)} AS _key FROM ({sql})"


def ordered_sql(layout: str, source: str, cols: list[str], b: int, stats: list[dict], hash_cols: list[str]) -> str:
    """The rows of ``source`` in ``layout``'s order, with the source's columns only."""
    if layout == "generated":
        return f"SELECT * FROM {source}"
    if layout == "hash":
        return f"SELECT * FROM {source} ORDER BY hash({', '.join(q(c) for c in hash_cols)})"
    if layout == "lexicographic":
        return f"SELECT * FROM {source} ORDER BY {', '.join(q(c) for c in cols)}"
    return f"SELECT * EXCLUDE (_key) FROM ({keyed_sql(layout, source, cols, b, stats)}) ORDER BY _key"
