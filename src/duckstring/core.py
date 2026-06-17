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
    """Decorator that registers a function as a Ripple in a Pond.

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


def trickle(func=None, *, pk=None, parents=None, name=None):
    """Decorator that registers a function as a **Trickle** — a history-preserving (incremental) Ripple.

    A Trickle is orchestrated exactly like a Ripple (a node in the package graph); it differs only in
    *I/O*: it writes via ``pond.append_table`` / ``pond.merge_table`` (history + changelog) and reads a
    source's change-set via ``pond.read_delta`` instead of a wholesale overwrite. ``pk`` declares the
    output primary key (identity for merge + downstream delta consumption) — required for a merge write,
    and the default a write inherits when it doesn't pass its own ``pk=``.

    Usage:
        @trickle(pk=("order_id", "line_no"))
        def priced_line(pond): ...
    """
    from .trickle_io import normalize_pk

    def make(f):
        _RIPPLES.append({
            "func": f, "name": name or f.__name__, "parents": parents or [],
            "trickle": {"pk": normalize_pk(pk)},
        })
        return f

    return make(func) if func is not None else make


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
        trickle: dict | None = None,
    ) -> None:
        from .engine.core import NEVER

        self.name = name
        self.version = version
        self.con = con
        self.root = root
        # Which major line of each Source this Pond consumes (from its pond.toml [sources] pins).
        # None/missing falls back to the flat puddles layout (local runs have no majors).
        self.source_majors = source_majors or {}
        # Trickle metadata for the running ripple (``{"pk": (...)}``) — the default PK its incremental
        # writes inherit. ``{}`` for a plain Ripple (the incremental write API still works if given a pk).
        self._trickle = trickle or {}
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
                    select = dp.read_select(data_dir, table)
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

        resolved = normalize_pk(pk) if pk is not None else tuple(self._trickle.get("pk", ()))
        return resolved

    def append_table(self, name: str, relation, *, pk=None, retain_t=None, retain_n=None) -> None:
        """Append ``relation`` to the history table ``name`` (insert-only; each row stamped with the
        run's freshness ``pond.f``). The fast path for event/fact logs whose identity is unique by
        construction — no PK check, no diff, no deletes; idempotent on replay at the same ``f``.
        ``retain_t`` (a ``timedelta``) / ``retain_n`` (a count) opt into bounding the kept history."""
        from . import trickle_io as trickle

        trickle.append_table(
            self.con, name, relation, self.f, self._resolve_pk(pk),
            retain_t=retain_t, retain_n=retain_n,
        )

    def merge_table(
        self, name: str, relation, *, comprehensive: bool = True, deletes=None, pk=None,
        retain_t=None, retain_n=None,
    ) -> None:
        """Upsert ``relation`` into the clean main table ``name`` + its changelog, stamped ``pond.f``.

        ``comprehensive=True`` (default, safe): ``relation`` is the *complete* current state — Duckstring
        diffs it against the prior state to derive inserts/updates/deletes automatically. ``comprehensive
        =False`` (expert): ``relation`` is a *partial* change-set and ``deletes`` the explicit PK removals
        (over-merge is idempotent-safe; **under-merge silently corrupts** — hence comprehensive default).
        ``retain_t`` / ``retain_n`` opt into bounding the kept changelog (the main is never trimmed)."""
        from . import trickle_io as trickle

        trickle.merge_table(
            self.con, name, relation, self.f, self._resolve_pk(pk),
            comprehensive=comprehensive, deletes=deletes, retain_t=retain_t, retain_n=retain_n,
        )

    def read_delta(self, ref: str):
        """A Source's change-set over this run's window ``(pond.previous_f, pond.f]`` — a
        :class:`~duckstring.trickle_io.Delta` (``.upserts`` / ``.deletes`` / ``.keys()``). Resolves the
        source's declared mode (append → history window; merge → changelog collapsed per PK; overwrite →
        full read) and falls back to a full read on a coverage miss / bootstrap."""
        from . import trickle_io as trickle
        from .dataplane import get_data_plane

        if "." not in ref:
            raise ValueError(f"read_delta needs a 'source.table' reference, got '{ref}'")
        source_pond, table = ref.split(".", 1)
        data_dir = self._source_data_dir(source_pond)
        dp = get_data_plane()
        dp.prepare(self.con)
        return trickle.read_delta(self.con, data_dir, table, self.previous_f, self.f, dp=dp)

    def keys_joining(self, spine_ref: str, delta, *, on):
        """The PKs of the full Source ``spine_ref`` whose ``on`` column(s) match ``delta.keys()`` — i.e.
        which of the spine's output keys a change in this dimension ripples to (the partial-path
        ``comprehensive=False`` helper). ``on`` equi-joins the spine column(s) to the **delta source's
        full PK** (delete propagation depends on the delta side being its PK); a non-PK-arity ``on`` is
        rejected."""
        from .trickle_io import KeySet, load_sidecar, unique_name

        on_cols = (on,) if isinstance(on, str) else tuple(on)
        source_pond, table = spine_ref.split(".", 1)
        meta = load_sidecar(self._source_data_dir(source_pond)).get(table, {})
        spine_pk = tuple(meta.get("pk", ()))
        if not spine_pk:
            raise ValueError(f"keys_joining: spine '{spine_ref}' has no declared primary key")
        keyset = delta.keys()  # a KeySet over the delta source's PK
        if len(on_cols) != len(keyset.pk):
            raise ValueError(
                f"keys_joining: 'on' has {len(on_cols)} column(s) but the delta source's PK has "
                f"{len(keyset.pk)} — 'on' must equi-join the spine to the delta source's full PK"
            )
        sview, dview = unique_name("spine"), unique_name("dkeys")
        self.read_table(spine_ref).create_view(sview, replace=True)
        keyset.create_view(dview)
        cond = " AND ".join(
            f's."{sc}" = d."{dc}"' for sc, dc in zip(on_cols, keyset.pk, strict=True)
        )
        pk_sel = ", ".join(f's."{c}"' for c in spine_pk)
        # Materialise the (small) key result into a uniquely-named temp so it doesn't re-bind to a later
        # keys_joining call's transient views (a second join edge would otherwise corrupt the first).
        result = unique_name("kj")
        self.con.execute(
            f'CREATE OR REPLACE TEMP TABLE "{result}" AS '
            f'SELECT DISTINCT {pk_sel} FROM "{sview}" s JOIN "{dview}" d ON {cond}'
        )
        return KeySet(self.con, self.con.sql(f'SELECT * FROM "{result}"'), spine_pk)

    def trickle(self, spine_ref: str, *, p: float = 0.3):
        """Start a :class:`~duckstring.trickle_builder.TrickleBuilder` rooted at the **spine** source
        ``spine_ref`` (the one owning the output PK) — the optional sugar over the partial-merge helpers.
        Chain ``.join(pond.trickle(dim), on=…)`` / ``.filter(...)`` / ``.select(...)`` then ``.merge(name)``;
        the builder propagates every join edge automatically, so (unlike hand-composed ``keys_joining``)
        it can't silently under-merge. Unsupported ops raise at build time.

        ``p`` is this source's **change-fraction threshold**: if its delta touches more than ``p`` of the
        source's current keys, the incremental slice stops paying off (a partial recompute + merge + a big
        changelog costs more than one clean pass), so ``.merge()`` falls back to a full comprehensive
        recompute for that run. It's **per source** — set it where you want the guard: ``p=0.3`` (default)
        caps a source that drives the output row count; ``p=1.0`` disables the check (always go incremental,
        skipping even the count) for a source you know rarely matches the other side. Applies to the spine
        (``pond.trickle(spine, p=…)``) and independently to each joined dimension
        (``.join(pond.trickle(dim, p=…), on=…)``)."""
        from .trickle_builder import TrickleBuilder

        return TrickleBuilder(self, spine_ref, p=p)

    def affected_groups(self, delta, *, by):
        """The distinct ``by`` group keys touched by ``delta`` — the aggregation sibling of
        :meth:`keys_joining`. Re-aggregate just these groups from the full input, then merge.

        Upserts carry their ``by`` columns; deletes carry only the PK, so a delete contributes its group
        **only when ``by`` ⊆ the delta source's PK** (then the deleted row's group key is known). If ``by``
        isn't a subset of the PK, deletes can't supply their group here — a deletion that empties a group
        not otherwise touched would be missed, so use ``by`` ⊆ PK or fall back to ``comprehensive=True``."""
        from .trickle_io import DeltaError, KeySet

        by_cols = (by,) if isinstance(by, str) else tuple(by)
        missing = [c for c in by_cols if c not in delta.upserts.columns]
        if missing:
            raise DeltaError(f"affected_groups: by column(s) {missing} are not in the delta's upserts")
        proj = ", ".join(f'"{c}"' for c in by_cols)
        groups = delta.upserts.project(proj)
        if set(by_cols) <= set(delta.pk):  # deletes carry the PK → their group key is known
            groups = groups.union(delta.deletes.project(proj))
        return KeySet(self.con, groups.distinct(), by_cols)


class Ripple:
    # TODO: runtime wrapper around a registered ripple function — name, func, parents list
    pass


class Trickle:
    # TODO: deferred — incremental/stateful Ripple variant with watermarks and merge semantics
    pass
