from __future__ import annotations

import time
from pathlib import Path

_RIPPLES: list[dict] = []
_PUDDLES: list[dict] = []


def retry_on_lock(fn, attempts: int = 12, base: float = 0.05):
    """Run ``fn``, retrying on transient DuckDB lock/conflict errors so concurrent writers *queue*
    (back off and retry) rather than crashing. Covers the catalog write-write conflict, the read-only/
    read-write config clash, and the cross-process file lock. Re-raises after the last attempt."""
    import duckdb

    for i in range(attempts):
        try:
            return fn()
        except (duckdb.TransactionException, duckdb.IOException, duckdb.ConnectionException):
            if i == attempts - 1:
                raise
            time.sleep(min(base * (2**i), 0.5))


def ripple(func=None, *, parents=None, name=None):
    """Decorator that registers a function as a Ripple — a named unit of code in a Pond. A Ripple has no
    tabular expectations: it may write zero, one, or many tables (in call order — sequential within the
    Ripple; split across Ripples for parallelism), or none at all. ``parents`` are the *within-Pond*
    Ripples it runs after, given by function reference; cross-Pond dependencies are declared in
    ``pond.toml [sources]``, not here.

    Incremental I/O is a capability, not a separate node type: any Ripple may read a Source's change-set
    (:meth:`Pond.read_delta` / :meth:`Pond.trickle`) and publish history-preserving **Trickle** tables
    (:meth:`Pond.append_table` / :meth:`Pond.merge_table`) — the mode is chosen per write.

    Usage:
        @ripple
        def load(pond): ...

        @ripple(parents=[load])
        def clean(pond): ...
    """
    if func is not None:
        # Called as @ripple without arguments
        _RIPPLES.append({"func": func, "name": name or func.__name__, "parents": parents or []})
        return func

    # Called as @ripple(...) with arguments
    def decorator(f):
        _RIPPLES.append({"func": f, "name": name or f.__name__, "parents": parents or []})
        return f

    return decorator


def collect_ripples() -> list[dict]:
    """Drain and return the current ripple registry. Used by the catchment at deploy time."""
    result = list(_RIPPLES)
    _RIPPLES.clear()
    return result


def puddle(target: str):
    """Decorator that registers a function as a Puddle — a local snapshot of the Source data it
    emulates, for testing a Pond before deployment (``duckstring pond hydrate`` / ``pond run``).

    Usage:
        @puddle("transactions.transaction")     # one table of a Source
        def transactions(p):
            p.write_table(p.con.sql("SELECT ..."))

        @puddle("products")                     # a whole Source (name each table)
        def products(p):
            p.write_table("product", p.con.sql("SELECT ..."))
    """

    def decorator(f):
        _PUDDLES.append({"func": f, "target": target, "name": f.__name__})
        return f

    return decorator


def collect_puddles() -> list[dict]:
    """Drain and return the current puddle registry. Used by ``duckstring pond hydrate``."""
    result = list(_PUDDLES)
    _PUDDLES.clear()
    return result


def read_pond_toml(pond_dir: Path) -> dict:
    """Parse ``pond.toml`` in ``pond_dir``; ``{}`` if absent."""
    import sys

    toml_path = Path(pond_dir) / "pond.toml"
    if not toml_path.exists():
        return {}
    text = toml_path.read_text(encoding="utf-8")
    if sys.version_info >= (3, 11):
        import tomllib

        return tomllib.loads(text)
    import tomli

    return tomli.loads(text)


def pond_entrypoints(info: dict) -> tuple[str, str]:
    """The (ripples, puddles) entrypoint paths declared in pond.toml, with the standard defaults."""
    pond = info.get("pond", {})
    return pond.get("ripples", "src/pond.py"), pond.get("puddles", "src/puddles.py")


def import_pond_module(source_dir: Path, entry: str):
    """Import the module at ``source_dir/entry`` for its decorator side-effects (``@ripple`` /
    ``@puddle``) and return it. The import is isolated: ``sys.path`` gains only the entry's parent
    for the duration, and any modules the import added are evicted afterwards so the next Pond's
    code never sees stale state."""
    import importlib
    import sys

    entry_path = Path(source_dir) / entry
    parent = str(entry_path.parent)
    stem = entry_path.stem
    before = set(sys.modules.keys())
    sys.path.insert(0, parent)
    try:
        sys.modules.pop(stem, None)
        importlib.invalidate_caches()
        return importlib.import_module(stem)
    finally:
        if parent in sys.path:
            sys.path.remove(parent)
        for key in list(sys.modules):
            if key not in before:
                sys.modules.pop(key, None)


def resolve_catchment_url(name: str | None = None) -> str:
    """A Catchment URL from a name in ``~/.duckstring/config.toml`` (default Catchment when ``None``),
    or the value itself when it already looks like a URL. Raises ``ValueError`` when unresolvable —
    no typer here; the CLI formats the message."""
    return resolve_catchment_auth(name)[0]


def resolve_catchment_auth(name: str | None = None) -> tuple[str, dict[str, str]]:
    """``(url, auth_headers)`` for a registered Catchment — see :func:`resolve_catchment_url`. The
    headers merge the registration's custom ``headers`` table with its ``key`` (as a Bearer
    Authorization). A bare URL resolves with no headers."""
    if name and "://" in name:
        return name, {}
    from .cli.config import auth_headers, load_config

    config = load_config()
    catchments = config.get("catchments", {})
    effective = name or config.get("default_catchment")
    if not effective and len(catchments) == 1:
        effective = next(iter(catchments))
    if not effective or effective not in catchments:
        raise ValueError(
            f"no catchment {name!r} registered" if name else "no catchment specified and no default set"
        )
    cfg = catchments[effective]
    return cfg["url"], auth_headers(cfg)


class Catchment:
    """Client-side handle for a Catchment server's read surface (the ``/api/query`` route).

    ``query``/``get`` return DuckDB relations materialised on ``con`` (each Pond's exported Parquet
    is queryable under ``"{pond}"."{table}"`` or bare). Inside a puddle definition, ``p.catchment()``
    returns one of these pre-bound to the puddle's Source and scratch connection."""

    def __init__(
        self, url: str, con=None, default_pond: str | None = None, default_table: str | None = None,
        api_key: str | None = None, headers: dict[str, str] | None = None,
    ):
        self.url = url.rstrip("/")
        # Auth attached to every request: custom headers (platform gates like Posit Connect), with
        # api_key as Bearer-Authorization sugar when no explicit Authorization header is given.
        self.headers = dict(headers or {})
        if api_key and not any(h.lower() == "authorization" for h in self.headers):
            self.headers["Authorization"] = f"Bearer {api_key}"
        self._con = con
        self._default_pond = default_pond
        self._default_table = default_table

    @property
    def con(self):
        if self._con is None:
            import duckdb

            self._con = duckdb.connect()
        return self._con

    def _pond(self, pond: str | None) -> str:
        target = pond or self._default_pond
        if not target:
            raise ValueError("no Pond given — pass pond=... or use this client from a puddle definition")
        return target

    def _post_query(self, payload: dict):
        import httpx

        resp = httpx.post(
            f"{self.url}/api/query", json=payload, headers=self.headers, timeout=httpx.Timeout(60.0, connect=5.0)
        )
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text[:300]
            raise RuntimeError(f"Catchment query failed ({resp.status_code}): {detail}")
        return resp

    def query(self, sql: str, pond: str | None = None):
        """Run ``sql`` against a Pond's exported tables; returns a DuckDB relation on ``con``."""
        import tempfile

        resp = self._post_query({"pond": self._pond(pond), "sql": sql, "format": "parquet"})
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
            f.write(resp.content)
            tmp = f.name
        return self.con.read_parquet(tmp)

    def get(self, table: str | None = None, pond: str | None = None):
        """Fetch a whole table; returns a DuckDB relation on ``con``."""
        target_table = table or self._default_table
        if not target_table:
            raise ValueError("no table given — pass table=... or define the puddle on a 'source.table' target")
        return self.query(f'SELECT * FROM "{target_table}"', pond=pond)

    def tables(self, pond: str | None = None) -> list[str]:
        """The names of a Pond's exported tables."""
        rows = self._post_query({"pond": self._pond(pond), "sql": "SHOW TABLES"}).json()
        return [row["name"] for row in rows]


class Puddle:
    """Handle passed to ``@puddle`` definitions. ``path`` is the puddle's destination directory —
    the general escape hatch (write models, blobs, anything there directly); ``write_table`` /
    ``write_path`` are conveniences layered on it."""

    def __init__(self, target: str, root: Path, default_catchment: str | None = None):
        self.target = target
        source, _, table = target.partition(".")
        self.source = source
        self.table = table or None
        self.root = Path(root)
        self.default_catchment = default_catchment
        self._con = None

    @property
    def path(self) -> Path:
        """The destination directory (``puddles/ponds/{source}/data/``), created on access."""
        dest = self.root / "ponds" / self.source / "data"
        dest.mkdir(parents=True, exist_ok=True)
        return dest

    @property
    def con(self):
        """A scratch in-memory DuckDB connection."""
        if self._con is None:
            import duckdb

            self._con = duckdb.connect()
        return self._con

    def write_table(self, name_or_relation, relation=None) -> Path:
        """Export a relation to ``{path}/{table}.parquet`` (atomic tmp+replace). The single-argument
        form uses the table named on the decorator; name the table explicitly for whole-Source puddles."""
        if relation is None:
            name, relation = self.table, name_or_relation
            if name is None:
                raise ValueError(
                    f"puddle '{self.target}' covers a whole Source — name the table: p.write_table(name, relation)"
                )
        else:
            name = name_or_relation
        if not hasattr(relation, "write_parquet"):
            relation = self.con.from_df(relation)
        dest = self.path / f"{name}.parquet"
        tmp = self.path / f"{name}.parquet.tmp"
        relation.write_parquet(str(tmp))
        tmp.replace(dest)
        return dest

    def write_path(self, src) -> None:
        """Copy data file(s) into the puddle: a parquet/csv path or glob. A single-table puddle reads
        everything matched as that table; a whole-Source puddle names each file's stem as a table."""
        src = Path(src).expanduser()
        if self.table is not None:
            self.write_table(self._read_path(src))
            return
        files = sorted(src.parent.glob(src.name)) if any(ch in src.name for ch in "*?[") else [src]
        if not files:
            raise FileNotFoundError(f"puddle '{self.target}': nothing matches {src}")
        for f in files:
            self.write_table(f.stem, self._read_path(f))

    def _read_path(self, src: Path):
        suffix = src.suffix.lower() or Path(src.name.split("*")[0]).suffix.lower()
        if suffix == ".csv":
            return self.con.read_csv(str(src))
        return self.con.read_parquet(str(src))

    def catchment(self, name: str | None = None) -> Catchment:
        """A :class:`Catchment` client bound to this puddle's Source and scratch connection."""
        url, headers = resolve_catchment_auth(name or self.default_catchment)
        return Catchment(url, con=self.con, default_pond=self.source, default_table=self.table, headers=headers)


class Pond:
    def __init__(
        self, name: str, version: str, con, root,
        source_majors: dict[str, int] | None = None, f=None, previous_f=None,
    ) -> None:
        from .engine.core import NEVER

        self.name = name
        self.version = version
        self.con = con
        self.root = root
        # Which major line of each Source this Pond consumes (from its pond.toml [sources] pins).
        # None/missing falls back to the flat puddles layout (local runs have no majors).
        self.source_majors = source_majors or {}
        # The run's freshness F (tz-aware UTC datetime): the ideal watermark/provenance stamp —
        # stable across crash recovery and retries, which all re-run at the same F (wall-clock
        # would differ per attempt). Local (puddle) runs stamp the run's start time.
        self.f = f
        # The previous successfully-completed run's freshness — the lower bound of the bracket
        # ``(previous_f, f]`` a ripple can read from a Source for hand-rolled incremental logic.
        # ``NEVER`` on the first run (so that bracket reads everything). Trickle will automate this.
        self.previous_f = NEVER if previous_f is None else previous_f

    def write_table(self, name: str, relation) -> None:
        tmp = f"__tmp_{name}"

        def _write() -> None:
            self.con.execute("BEGIN TRANSACTION")
            try:
                self.con.execute(f'DROP TABLE IF EXISTS "{tmp}"')
                relation.create(f'"{tmp}"')
                self.con.execute(f'DROP TABLE IF EXISTS "{name}"')
                self.con.execute(f'ALTER TABLE "{tmp}" RENAME TO "{name}"')
                self.con.execute("COMMIT")
            except Exception:
                self.con.execute("ROLLBACK")  # release the txn so a retry starts clean
                raise

        retry_on_lock(_write)  # a concurrent write conflict queues + retries rather than failing

    def _source_data_dir(self, source_pond: str):
        """The published ``data_dir`` for a foreign Source, honouring this Pond's major pin (or the flat
        puddles layout in local runs, which have no majors)."""
        from pathlib import Path as _Path

        base = _Path(self.root) / "ponds" / source_pond
        major = self.source_majors.get(source_pond)
        return base / f"m{major}" / "data" if major is not None else base / "data"

    def read_table(self, ref: str):
        """A relation over a table — own (``"name"``) or a Source's (``"source.table"``). A Source
        table is also registered as a temp view under its own name, so SQL can reference it directly
        (``FROM table``). Prefer that over naming the returned relation's Python variable in SQL:
        that resolves by scanning Python frames, which is unreliable under the threaded executor.

        For a Trickle source this is the **clean current state** (the merge *main* / the full append
        history); its ``_duckstring_*`` system columns are projected out so the read is user-facing."""
        if "." in ref:
            source_pond, table = ref.split(".", 1)
            if source_pond != self.name:
                from .dataplane import get_data_plane
                from .trickle_io import _strip_system

                data_dir = self._source_data_dir(source_pond)
                dp = get_data_plane()
                dp.prepare(self.con)  # ready the connection to read the Source's published format
                try:
                    # As-of pin: read the Source snapshot at this run's freshness, NOT latest. A Pond Run
                    # spans wall-clock time over several Ripples; an upstream Source can republish mid-run.
                    # Pinning to `self.f` gives every Ripple the same consistent as-of-F view of the Source
                    # (no intra-run read skew / too-fresh data). Honoured by the Iceberg plane (retained
                    # snapshots); the Parquet plane has no history and reads latest regardless.
                    select = dp.read_select(data_dir, table, as_of=self.f)
                except FileNotFoundError as exc:
                    raise FileNotFoundError(
                        f"No exported data found for '{source_pond}.{table}' — "
                        f"has {source_pond} completed a successful run?"
                    ) from exc
                rel = _strip_system(self.con.sql(select))
                try:
                    rel.create_view(table, replace=True)
                except Exception:
                    pass  # name taken by one of this Pond's own tables — the relation still works
                return rel
            return self.con.sql(f'SELECT * FROM "{table}"')
        return self.con.sql(f'SELECT * FROM "{ref}"')

    # ─── Trickle: incremental I/O (see duckstring.trickle_io / plans/trickle.md) ───

    def _resolve_pk(self, pk):
        from .trickle_io import normalize_pk

        return normalize_pk(pk) if pk is not None else ()

    def append_table(
        self, name: str, relation, *, pk=None, validate_pk=False, retain_t=None, retain_n=None
    ) -> None:
        """Append ``relation`` to the history table ``name`` (insert-only; each row stamped with the
        run's freshness ``pond.f``). The fast path for event/fact logs whose identity is unique by
        construction — no diff, no deletes; idempotent on replay at the same ``f``. ``pk`` is optional
        (recorded as the table's declared key, for downstream/the data viewer); pass ``validate_pk=True``
        to assert that ``pk`` is unique across the appended rows and existing history (a per-write cost
        that trades speed for a correctness guarantee). ``retain_t`` (a ``timedelta``) / ``retain_n`` (a
        count) opt into bounding the kept history."""
        from . import trickle_io as trickle

        trickle.append_table(
            self.con, name, relation, self.f, self._resolve_pk(pk),
            validate_pk=validate_pk, retain_t=retain_t, retain_n=retain_n,
        )

    def merge_table(self, name: str, relation, *, pk, retain_t=None, retain_n=None) -> None:
        """Merge the **complete current state** ``relation`` into the clean main table ``name`` + its Z-set
        changelog, stamped ``pond.f``. ``pk`` (the output identity) is **required** — it is the merge key.
        Duckstring diffs ``relation`` against the prior main as a full-row Z-set difference to derive
        inserts/updates/deletes automatically — so it is always safe to hand it the whole state. ``retain_t``
        / ``retain_n`` opt into bounding the kept changelog (the main, being the clean current state, is
        never trimmed)."""
        from . import trickle_io as trickle

        trickle.merge_table(
            self.con, name, relation, self.f, self._resolve_pk(pk),
            retain_t=retain_t, retain_n=retain_n,
        )

    def apply_zset(self, name: str, zset, *, pk, retain_t=None, retain_n=None) -> None:
        """Apply a **Z-set** change (a relation of user columns + ``_duckstring_d``) to the output Trickle
        ``name`` — the low-level primitive the builder uses for the incremental path. ``pk`` (the output
        identity) is **required**. Prefer :meth:`trickle` / :meth:`merge_table`; reach for this only for
        hand-rolled incremental compute."""
        from . import trickle_io as trickle

        trickle.apply_zset(
            self.con, name, zset, self.f, self._resolve_pk(pk),
            retain_t=retain_t, retain_n=retain_n,
        )

    def read_delta(self, ref: str):
        """A Source's change over this run's window ``(pond.previous_f, pond.f]`` as a **Z-set** — a
        :class:`~duckstring.trickle_io.Delta` (``.zset`` + ``.is_full``; ``.upserts`` / ``.deletes`` are
        derived conveniences). Resolves the source's declared mode (append → history window all ``+1``;
        merge → changelog window consolidated; overwrite → full read if it advanced, else an empty delta)
        and falls back to a full read on a coverage miss / bootstrap."""
        from . import trickle_io as trickle
        from .dataplane import get_data_plane

        if "." not in ref:
            raise ValueError(f"read_delta needs a 'source.table' reference, got '{ref}'")
        source_pond, table = ref.split(".", 1)
        data_dir = self._source_data_dir(source_pond)
        dp = get_data_plane()
        dp.prepare(self.con)
        return trickle.read_delta(self.con, data_dir, table, self.previous_f, self.f, dp=dp)

    def trickle(self, spine_ref: str, *, p: float = 0.3):
        """Start a :class:`~duckstring.trickle_builder.TrickleBuilder` rooted at the **spine** source
        ``spine_ref``. Chain ``.join(pond.trickle(dim), on=…)`` / ``.filter(...)`` / ``.select(...)``
        then ``.merge(name, pk=…)`` (the merge key is the output identity). The builder composes each
        changed source's Z-set delta through
        the join (DBSP-style), so a join can be on **any** key and a deletion propagates by full-row
        retraction — there is one ``.join()`` and no FK=PK constraint. Any table is a valid source
        (Trickle or plain overwrite Ripple): an unchanged Ripple is a free stable operand, a changed one
        forces a comprehensive recompute.

        ``p`` is this source's **change-fraction threshold**: if its delta touches more than ``p`` of the
        source's current rows, the incremental slice stops paying off, so ``.merge()`` recomputes
        comprehensively for that run. Per source: ``p=0.3`` (default) caps a source that drives the output
        row count; ``p=1.0`` disables the check (and skips the count). Applies to the spine
        (``pond.trickle(spine, p=…)``) and each joined dimension (``.join(pond.trickle(dim, p=…), on=…)``)."""
        from .trickle_builder import TrickleBuilder

        return TrickleBuilder(self, spine_ref, p=p)


class Ripple:
    # TODO: runtime wrapper around a registered ripple function — name, func, parents list
    pass
