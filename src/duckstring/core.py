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


def ripple(func=None, *, parents=None, name=None, always_run=False):
    """Register a function as a Ripple.

    Use bare (``@ripple``) or with arguments (``@ripple(parents=[...])``). The function takes one argument,
    the :class:`Pond` handle, and its return value is ignored.

    Args:
        parents: Ripples in the same Pond that must finish before this one starts, as function
            references. Dependencies on other Ponds are declared in ``pond.toml``.
        name: The Ripple's name. Defaults to the function name.
        always_run: Run even when no Source has changed since the last Pond Run. If any Ripple in a Pond
            sets this, the whole Pond always runs.

    Example::

        @ripple
        def daily_sales(pond): ...

        @ripple(parents=[daily_sales])
        def join_lines(pond): ...

    Reference: https://docs.duckstring.com/reference/python/decorators
    """
    if func is not None:
        # Called as @ripple without arguments
        _RIPPLES.append({"func": func, "name": name or func.__name__, "parents": parents or [],
                         "always_run": always_run})
        return func

    # Called as @ripple(...) with arguments
    def decorator(f):
        _RIPPLES.append({"func": f, "name": name or f.__name__, "parents": parents or [],
                         "always_run": always_run})
        return f

    return decorator


def collect_ripples() -> list[dict]:
    """Drain and return the current ripple registry. Used by the catchment at deploy time."""
    result = list(_RIPPLES)
    _RIPPLES.clear()
    return result


def puddle(target: str):
    """Register a function that builds a Puddle: a local snapshot of Source data for testing.

    Args:
        target: ``"source.table"`` for one table of a Source, or ``"source"`` for a whole Source whose
            tables the function names itself.

    The function takes one argument, the :class:`Puddle` handle, and ``duckstring pond hydrate`` runs it.
    It can write through the handle or return the data: a relation is written as the target table, a path
    is copied in with :meth:`Puddle.write_path`. Targeting the Pond's own name provides its previous output.

    Example::

        @puddle("transactions.transaction")
        def transactions(p):
            p.write_table(p.con.sql("SELECT ..."))

        @puddle("products")
        def products(p):
            p.write_table("product", p.con.sql("SELECT ..."))

    Reference: https://docs.duckstring.com/reference/python/decorators
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


def parse_ref(ref: str) -> tuple[str | None, str]:
    """Split a table or Object reference into ``(source, name)``; ``source`` is ``None`` for a bare name.

    ``"name"`` is the Pond's own; ``"source.name"`` is a Source's, split at the first dot. A part in
    backticks is taken literally, so names containing dots can be referenced: ``"`model.pkl`"`` is the
    Pond's own ``model.pkl``, and ``"forecasting.`model.pkl`"`` is the Source ``forecasting``'s. Raises
    ``ValueError`` for an unclosed backtick or an empty part."""
    def bad(why: str) -> ValueError:
        return ValueError(f"invalid reference {ref!r}: {why}")

    def quoted(text: str) -> tuple[str, str]:  # text starts with a backtick → (inside, remainder)
        end = text.find("`", 1)
        if end == -1:
            raise bad("unclosed backtick")
        return text[1:end], text[end + 1:]

    if ref.startswith("`"):
        first, rest = quoted(ref)
    elif "." in ref:
        first, rest = ref[:ref.index(".")], ref[ref.index("."):]
    else:
        first, rest = ref, ""
    if not rest:
        if not first:
            raise bad("empty name")
        return None, first
    if not rest.startswith("."):
        raise bad("expected '.' after the quoted Source")
    second = rest[1:]
    if second.startswith("`"):
        second, tail = quoted(second)
        if tail:
            raise bad("unexpected text after the quoted name")
    if not first or not second:
        raise bad("empty Source or name")
    return first, second


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
    """A client for reading a Pond's published tables from a running Catchment.

    Results are DuckDB relations loaded on ``con``. Queries target the Pond's highest deployed major.

    Args:
        url: The Catchment's address.
        con: DuckDB connection for results. Defaults to a new in-memory connection.
        default_pond: Pond used when a method is called without ``pond``.
        default_table: Table used when :meth:`get` is called without ``table``.
        api_key: Sent as ``Authorization: Bearer <key>`` unless ``headers`` sets ``Authorization``.
        headers: Extra headers sent with every request.

    Inside a ``@puddle`` function, ``p.catchment()`` returns one configured for the Puddle's Source.

    Reference: https://docs.duckstring.com/reference/python/catchment
    """

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
        """Run read-only SQL against one Pond's published tables and return the result as a relation.

        Tables are referred to by bare name. ``pond`` defaults to ``default_pond``. Raises ``RuntimeError``
        when the Catchment returns an error, and ``ValueError`` when no Pond is given.
        """
        import tempfile

        resp = self._post_query({"pond": self._pond(pond), "sql": sql, "format": "parquet"})
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
            f.write(resp.content)
            tmp = f.name
        return self.con.read_parquet(tmp)

    def get(self, table: str | None = None, pond: str | None = None):
        """Fetch a whole table as a relation. ``table`` defaults to ``default_table``, ``pond`` to ``default_pond``."""
        target_table = table or self._default_table
        if not target_table:
            raise ValueError("no table given — pass table=... or define the puddle on a 'source.table' target")
        return self.query(f'SELECT * FROM "{target_table}"', pond=pond)

    def tables(self, pond: str | None = None) -> list[str]:
        """The names of a Pond's published tables."""
        rows = self._post_query({"pond": self._pond(pond), "sql": "SHOW TABLES"}).json()
        return [row["name"] for row in rows]


class Puddle:
    """The handle passed to every ``@puddle`` function.

    Attributes:
        con: A scratch in-memory DuckDB connection.
        path: The directory the Puddle writes into, ``puddles/ponds/{source}/data/``. Anything written
            here directly is visible to a local run.
        source: The Source name from the decorator target.
        table: The table name from the decorator target, or ``None`` for a whole-Source Puddle.

    Reference: https://docs.duckstring.com/reference/python/puddle
    """

    def __init__(self, target: str, root: Path, default_catchment: str | None = None):
        self.target = target
        source, table = parse_ref(target)
        if source is None:  # a whole-Source Puddle: the target names the Source
            source, table = table, None
        self.source = source
        self.table = table
        self.root = Path(root)
        self.default_catchment = default_catchment
        self._con = None

    @property
    def path(self) -> Path:
        """The directory the Puddle writes into, ``puddles/ponds/{source}/data/``. Created on access."""
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
        """Write a relation to ``{path}/{name}.parquet`` and return the file path.

        Call as ``write_table(relation)`` to use the table named in the decorator target, or
        ``write_table(name, relation)``. A whole-Source Puddle must name each table. A pandas DataFrame is
        accepted and converted through ``con``.
        """
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
        """Copy existing Parquet or CSV files into the Puddle from a path or glob.

        For a single-table Puddle, everything matched becomes that table. For a whole-Source Puddle, each file
        becomes a table named after the file. Raises ``FileNotFoundError`` when a glob matches nothing.
        """
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

    def write_object(self, name: str, src) -> None:
        """Seed an Object for the Source, so a Ripple reading ``"{source}.{name}"`` finds it.

        ``src`` is a file or directory path, ``bytes``, or a binary file-like.
        """
        from .objects import write_object_now

        write_object_now(self.path, name, src)

    def read_object(self, name: str) -> bytes:
        """The bytes of a seeded single-file Object."""
        from .objects import read_object as _read

        return _read(self.path, name)

    def object_path(self, name: str) -> Path:
        """A local path to a seeded Object, file or directory."""
        from .objects import object_path as _path

        return _path(self.path, name, self.path / ".object_cache")

    def catchment(self, name: str | None = None) -> Catchment:
        """A :class:`Catchment` client whose default Pond is this Puddle's Source and default table is its target
        table, sharing ``con``. ``name`` is a registered Catchment name or a URL, defaulting to the default
        Catchment.
        """
        url, headers = resolve_catchment_auth(name or self.default_catchment)
        return Catchment(url, con=self.con, default_pond=self.source, default_table=self.table, headers=headers)


class MissingSourceAsset(FileNotFoundError):
    """A Source table or Object that a Ripple read has not been published.

    Duckstring treats this as waiting rather than failing: the Pond is held until the Source publishes
    again, with no retry spent and no alert sent. A subclass of ``FileNotFoundError``.
    """

    def __init__(self, source: str, table: str) -> None:
        self.source = source
        self.table = table
        super().__init__(
            f"'{source}.{table}' is not published — waiting for '{source}' to (re)publish it "
            f"(has it completed a run that produces '{table}'?)"
        )


class Pond:
    """The handle passed to every Ripple.

    Attributes:
        con: DuckDB connection to the Pond's own database, shared by its Ripples. Tables written here
            are published when the Pond Run succeeds.
        name: The Pond's name.
        version: The deployed version.
        f: The freshness of this Pond Run (timezone-aware UTC). Stable across retries and crash recovery
            of the same run. In a local run, the run's start time.
        previous_f: The freshness of the previous successful run, or ``datetime.min`` (UTC) on the first.

    Table and Object references are ``"name"`` for this Pond's own and ``"source.name"`` for a Source's,
    split at the first dot; put a name containing dots in backticks (``"sales.`daily.v2`"``). See
    :func:`parse_ref`.

    Reference: https://docs.duckstring.com/reference/python/pond
    """

    def __init__(
        self, name: str, version: str, con, root,
        source_majors: dict[str, int] | None = None, source_f: dict[str, str] | None = None,
        f=None, previous_f=None, data_root: str | None = None,
        sources_changed: bool = True, skip_sink=None, staging_dir=None, own_data_dir=None,
        flock: str | None = None, sources=None,
    ) -> None:
        from .engine.core import NEVER

        # The declared Sources, when known (a deployed run's pins, or a local run's pond.toml), so a
        # reference to anything else fails clearly instead of waiting on a Source that will never publish.
        declared = sources if sources is not None else (source_majors or None)
        self._declared_sources = set(declared) if declared is not None else None

        # No-change skip (plans/no-change-skip.md): ``sources_changed`` is the engine's verdict for this
        # Run; ``skip_sink`` is the Duck's callback to mark the Run a pass when ``skip()`` is called.
        # Both default to the always-changed / no-op behaviour for local (puddle) runs.
        self._sources_changed = sources_changed
        self._skip_sink = skip_sink
        # Non-tabular Objects (see objects.py / plans/objects.md). ``staging_dir`` is the local dir a
        # ``write_object`` stages into (committed at export by the runtime); ``own_data_dir`` is where this
        # Pond's own Objects are published (for own reads). Both set by the runtime; None outside a run.
        self._staging_dir = staging_dir
        self._own_data_dir = own_data_dir
        self._object_scratch = None
        # The Pond's resolved Flock posture (off|upgrade|always) — from the Pond's config, threaded in
        # by the runtime (the Duck sets it from its config env); the trickle terminals read it.
        self.flock = flock
        self.name = name
        self.version = version
        self.con = con
        self.root = root
        # Where the data plane publishes/reads tables — a local path under the state root by default, or an
        # object-store / Volume URI (``DUCKSTRING_DATA_ROOT``). Foreign-Source reads resolve through it.
        self.data_root = data_root
        # Which major line of each Source this Pond consumes (from its pond.toml [sources] pins).
        # None/missing falls back to the flat puddles layout (local runs have no majors).
        self.source_majors = source_majors or {}
        # The freshness the Catchment says each Source has published — lets a read reject a stale
        # local publish (see catchment.registry.resolve_data_dir). Absent for puddle runs.
        self.source_f = source_f or {}
        # The run's freshness F (tz-aware UTC datetime): the ideal watermark/provenance stamp —
        # stable across crash recovery and retries, which all re-run at the same F (wall-clock
        # would differ per attempt). Local (puddle) runs stamp the run's start time.
        self.f = f
        # The previous successfully-completed run's freshness — the lower bound of the bracket
        # ``(previous_f, f]`` a ripple can read from a Source for hand-rolled incremental logic.
        # ``NEVER`` on the first run (so that bracket reads everything). Trickle will automate this.
        self.previous_f = NEVER if previous_f is None else previous_f
        # Observed table-level lineage (plans/lineage.md Phase 1): every read/write this handle brokers,
        # recorded as it happens — exact, never inferred. Reads are (source_pond|None, table); writes are
        # this Pond's own output names. Drained per Ripple Run by the executor (:meth:`take_lineage`) and
        # shipped on the ripple event; a plain dict/sets so recording costs nothing measurable.
        self._lineage: dict[str, set] = {"reads": set(), "writes": set()}

    # ─── observed lineage (plans/lineage.md) ───────────────────────────────────

    def _record_read(self, source: str | None, table: str) -> None:
        self._lineage["reads"].add((source, table))

    def record_lineage_write(self, name: str) -> None:
        """Record ``name`` as an output this run wrote — the optional host hook the Trickle builder's
        terminals call (its writes go straight to the registry, not through the handle's write methods)."""
        self._lineage["writes"].add(name)

    def take_lineage(self) -> dict:
        """Drain the recorded lineage as a JSON-able ``{"reads": [[source|None, table], …], "writes":
        [name, …]}`` (sorted for stable payloads) and reset — called by the executor per Ripple Run."""
        out = {
            "reads": sorted(([s, t] for s, t in self._lineage["reads"]),
                            key=lambda p: (p[0] or "", p[1])),  # own reads (None) sort first
            "writes": sorted(self._lineage["writes"]),
        }
        self._lineage = {"reads": set(), "writes": set()}
        return out

    def sources_changed(self) -> bool:
        """Whether any Source's output changed since this Pond last ran.

        Mainly for a Ripple declared with ``always_run=True``, which runs regardless and can use this to skip
        its data work::

            send_heartbeat()
            if not pond.sources_changed():
                pond.skip()
                return

        Always ``True`` in a local run.
        """
        return self._sources_changed

    def skip(self) -> None:
        """Mark this Pond Run as producing no change.

        Downstream Ponds then treat this Pond's output as unchanged and can skip their own runs. Freshness
        still advances. Has no effect in a local run.
        """
        if self._skip_sink is not None:
            self._skip_sink()

    def write_table(self, name: str, relation) -> None:
        """Replace the table ``name`` in the Pond's database with ``relation``, in one transaction.

        Every table in the database is published when the whole Pond Run succeeds; if any Ripple fails,
        nothing from the run is published. A write that collides with another Ripple's is retried. Tables whose
        names start with ``_duckstring_`` are never published, and columns with that prefix are rejected at
        publish. Use :meth:`append_table` or :meth:`merge_table` to keep history for incremental consumers.
        """
        self.record_lineage_write(name)
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

    def write_object(self, name: str, src) -> None:
        """Stage a non-tabular Object (a model, a file, a directory) to be published with the run's tables.

        ``name`` is letters, digits and underscores, optionally with single dots between parts
        (``model.pkl``); read a dotted name back with backticks, ``read_object("`model.pkl`")``.

        ``src`` is a file or directory path, ``bytes``, or a binary file-like. The Object is replaced as one
        unit when the run publishes; a later Ripple failure leaves the previous Object in place. Raises
        ``RuntimeError`` outside a Pond Run.
        """
        from .objects import stage_object

        if self._staging_dir is None:
            raise RuntimeError("write_object is only available inside a Pond Run (no staging context)")
        self.record_lineage_write(name)
        stage_object(Path(self._staging_dir), name, src)

    def read_object(self, ref: str) -> bytes:
        """The bytes of a single-file Object: ``"name"`` for this Pond's own, ``"source.name"`` for a Source's.

        An own Object staged earlier in this run is returned in preference to the published one. Raises
        ``ObjectError`` for a directory Object; use :meth:`object_path` instead.
        """
        from .objects import read_object as _read
        from .objects import read_staged

        source, name = self._split_object_ref(ref)
        self._record_read(source, name)
        if source is None:
            if self._staging_dir is not None:
                staged = read_staged(Path(self._staging_dir), name)
                if staged is not None:
                    return staged
            return _read(self._own_store(), name)
        return _read(self._source_data_dir(source), name)

    def object_path(self, ref: str) -> Path:
        """A local path to an Object, file or directory: ``"name"`` or ``"source.name"``.

        An Object in object storage is downloaded once per run to a scratch directory. Treat the path as
        read-only.
        """
        from .objects import object_path as _path
        from .objects import staged_object_path

        source, name = self._split_object_ref(ref)
        self._record_read(source, name)
        if source is None:
            if self._staging_dir is not None:
                sp = staged_object_path(Path(self._staging_dir), name)
                if sp is not None:
                    return sp
            return _path(self._own_store(), name, self._scratch())
        return _path(self._source_data_dir(source), name, self._scratch())

    def _resolve_ref(self, ref: str, what: str = "table") -> tuple[str | None, str]:
        """:func:`parse_ref`, with this Pond's own name as the Source mapped to ``None`` (own), and a clear
        error for a Source this Pond doesn't declare, suggesting backticks for a dotted own name."""
        source, name = parse_ref(ref)
        if source == self.name:
            return None, name
        if source is not None and self._declared_sources is not None and source not in self._declared_sources:
            declared = ", ".join(sorted(self._declared_sources)) or "none"
            hint = f' To read this Pond\'s own {what} named "{ref}", quote it: "`{ref}`".' if "`" not in ref else ""
            raise ValueError(f"'{source}' is not a Source of '{self.name}' (declared: {declared}).{hint}")
        return source, name

    def _split_object_ref(self, ref: str):
        return self._resolve_ref(ref, "Object")

    def _own_store(self):
        from .storage import LocalStorage, Storage

        if self._own_data_dir is None:
            raise RuntimeError("read_object/object_path need a run context (no own data dir)")
        return self._own_data_dir if isinstance(self._own_data_dir, Storage) else LocalStorage(Path(self._own_data_dir))

    def _scratch(self) -> Path:
        import tempfile

        if self._object_scratch is None:
            self._object_scratch = Path(tempfile.mkdtemp(prefix="duckstring-obj-"))
        return self._object_scratch

    def _source_data_dir(self, source_pond: str):
        """The published data location for a foreign Source as a :class:`~duckstring.storage.Storage`,
        honouring this Pond's major pin and the configured data root (or the flat puddles layout in local
        runs, which have no majors). Resolved **local-first** (plans/persist.md): a Source co-located on
        this Pool published locally — read that (the fast handoff, no object-store round trip); only a
        Source with no local publish is read from the data root."""
        from pathlib import Path as _Path

        from .storage import LocalStorage

        major = self.source_majors.get(source_pond)
        if major is None:  # flat puddles layout (local runs have no majors) — always under the local root
            return LocalStorage(_Path(self.root) / "ponds" / source_pond / "data")
        from .catchment.registry import resolve_data_dir

        return resolve_data_dir(_Path(self.root), source_pond, major, self.data_root,
                                expected_f=self.source_f.get(source_pond))

    def read_table(self, ref: str):
        """A relation over a table's current contents: ``"table"`` or ``"source.table"``.

        A Source table is also registered as a view under its bare name, so SQL can refer to it directly
        (``FROM product``), unless one of this Pond's own tables has that name. Source reads are pinned to this
        run's freshness where the data plane keeps history, so every Ripple in a run sees the same snapshot.
        For a Trickle, the result is the current state without the ``_duckstring_*`` system columns.

        Refer to the registered view name in SQL rather than to a Python variable holding the relation, which
        is unreliable under the Duck's threaded executor.

        Raises :class:`MissingSourceAsset` when a Source table isn't published.
        """
        source_pond, table = self._resolve_ref(ref)
        if source_pond is not None:
            from .dataplane import get_data_plane
            from .trickle_io import _strip_system

            self._record_read(source_pond, table)
            data_dir = self._source_data_dir(source_pond)
            dp = get_data_plane()
            dp.prepare(self.con)  # ready the connection to read the Source's published format
            data_dir.duckdb_setup(self.con)  # object store → httpfs + credentials (no-op for local)
            try:
                # As-of pin: read the Source snapshot at this run's freshness, NOT latest. A Pond Run
                # spans wall-clock time over several Ripples; an upstream Source can republish mid-run.
                # Pinning to `self.f` gives every Ripple the same consistent as-of-F view of the Source
                # (no intra-run read skew / too-fresh data). Honoured by the Iceberg plane (retained
                # snapshots); the Parquet plane has no history and reads latest regardless.
                select = dp.read_select(data_dir, table, as_of=self.f)
            except FileNotFoundError as exc:
                raise MissingSourceAsset(source_pond, table) from exc
            rel = _strip_system(self.con.sql(select))
            from .trickle_io import _is_current_view

            if not _is_current_view(self.con, table):  # never replace this Pond's own merge Trickle view
                try:
                    rel.create_view(table, replace=True)
                except Exception:
                    pass  # name taken by one of this Pond's own tables — the relation still works
            return rel
        self._record_read(None, table)
        return self._own_current(table)

    def _own_current(self, name: str):
        """Read one of this Pond's own registry tables as its current clean state — a merge Trickle is
        reconstructed from its base ⊎ changelog (the main is log-structured); anything else is read directly."""
        from . import trickle_io as trickle

        return trickle.current_state(self.con, name)

    def count_table(self, ref: str) -> int:
        """The current number of rows in a table (``"table"`` or ``"source.table"``), read from metadata without
        scanning where possible.
        """
        from . import trickle_io as trickle

        source_pond, table = self._resolve_ref(ref)
        if source_pond is not None:
            from .dataplane import get_data_plane

            self._record_read(source_pond, table)
            data_dir = self._source_data_dir(source_pond)
            dp = get_data_plane()
            dp.prepare(self.con)
            data_dir.duckdb_setup(self.con)
            meta = trickle.load_sidecar(data_dir).get(table, {})
            (n,) = self.con.execute(
                dp.consolidated_count_select(data_dir, table, meta, as_of=self.f)
            ).fetchone()
            return int(n)
        return trickle.count_current(self.con, table)

    # ─── Trickle: incremental I/O (see duckstring.trickle_io / plans/trickle.md) ───

    def _resolve_pk(self, pk):
        from .trickle_io import normalize_pk

        return normalize_pk(pk) if pk is not None else ()

    def append_table(
        self, name: str, relation, *, pk=None, fail_on_conflict=True, retain_t=None, retain_n=None
    ) -> bool:
        """Append ``relation`` to the append Trickle ``name``, stamping each row with ``pond.f``.

        Args:
            name: Table name. Created on first use.
            relation: The new rows.
            pk: The table's primary key, recorded for consumers and checked when ``fail_on_conflict`` is set.
            fail_on_conflict: With ``pk`` set, raise before writing if the key is repeated within
                ``relation`` or already in the table's history. ``False`` skips the check.
            retain_t: A ``timedelta``; drop history rows stamped earlier than ``f - retain_t``.
            retain_n: Keep only the rows from the newest ``retain_n`` runs.

        Returns ``True`` if rows were appended. Safe to repeat at the same ``f``. Raises ``DeltaError`` on a
        key conflict or a missing key column.

        Reference: https://docs.duckstring.com/reference/python/trickle_io
        """
        from . import trickle_io as trickle

        self.record_lineage_write(name)
        return trickle.append_table(
            self.con, name, relation, self.f, self._resolve_pk(pk),
            fail_on_conflict=fail_on_conflict, retain_t=retain_t, retain_n=retain_n,
        )

    def merge_table(self, name: str, relation, *, pk, retain_t=None, retain_n=None,
                    compact_threshold=None) -> bool:
        """Merge the complete current state ``relation`` into the merge Trickle ``name``.

        Duckstring compares ``relation`` with the table's state before this run and records the inserts,
        updates and deletes in its change log. Pass the whole table every time: missing rows are recorded as
        deleted.

        Args:
            name: Table name.
            relation: The table's complete current state.
            pk: The primary key. Required, and must be unique in ``relation``.
            retain_t: A ``timedelta``; drop change-log rows stamped earlier than ``f - retain_t``.
            retain_n: Keep only the change-log rows from the newest ``retain_n`` runs.
            compact_threshold: Bytes the change log must reach before it is folded into the table's base.
                Defaults to ``DUCKSTRING_COMPACT_THRESHOLD`` (256 MiB).

        Returns ``True`` if the state changed, the usual signal for :meth:`skip`. Raises ``DeltaError`` for a
        missing or empty ``pk``.

        Within the Pond, ``name`` is a view over the current state (no system columns), so later Ripples can
        query it in SQL; the compacted base is ``{name}__base``.

        Reference: https://docs.duckstring.com/reference/python/trickle_io
        """
        from . import trickle_io as trickle

        self.record_lineage_write(name)
        return trickle.merge_table(
            self.con, name, relation, self.f, self._resolve_pk(pk),
            retain_t=retain_t, retain_n=retain_n, compact_threshold=compact_threshold,
        )

    def apply_zset(self, name: str, zset, *, pk, retain_t=None, retain_n=None,
                   compact_threshold=None) -> bool:
        """Append an already-computed change to the merge Trickle ``name`` without comparing it with the state.

        ``zset`` holds the table's columns plus ``_duckstring_d`` (``+1`` added, ``-1`` removed; an update is a
        ``-1`` of the old row and a ``+1`` of the new). It is consolidated before writing. ``pk`` is required;
        ``retain_t``, ``retain_n`` and ``compact_threshold`` are as for :meth:`merge_table`. Returns ``True`` if
        the consolidated change is non-empty.

        This is the write the Trickle builder uses. Prefer :meth:`trickle` or :meth:`merge_table`: a wrong
        weight corrupts the table for every consumer.
        """
        from . import trickle_io as trickle

        self.record_lineage_write(name)
        return trickle.apply_zset(
            self.con, name, zset, self.f, self._resolve_pk(pk),
            retain_t=retain_t, retain_n=retain_n, compact_threshold=compact_threshold,
        )

    def read_delta(self, ref: str):
        """The changes to a Source table (``"source.table"``) over this run's window ``(previous_f, f]``.

        Returns a ``Delta``: ``.zset`` (the Source's columns plus ``_duckstring_d``), ``.is_full``, and the
        conveniences ``.upserts`` and ``.deletes``.

        - Append Trickle: the rows appended in the window, all ``+1``.
        - Merge Trickle: the change-log rows in the window, consolidated to the net change.
        - Plain table: the whole table (``is_full``) if republished since ``previous_f``, else empty.
        - First run, or ``previous_f`` older than the Source's retained history: the whole table (``is_full``).

        A full read must be treated as a complete recompute, not an increment. Raises ``ValueError`` without a
        Source prefix and :class:`MissingSourceAsset` for an unpublished table.

        Reference: https://docs.duckstring.com/reference/python/trickle_io
        """
        from . import trickle_io as trickle
        from .dataplane import get_data_plane

        source_pond, table = self._resolve_ref(ref)
        if source_pond is None:
            raise ValueError(f"read_delta needs a 'source.table' reference, got '{ref}'")
        self._record_read(source_pond, table)
        data_dir = self._source_data_dir(source_pond)
        dp = get_data_plane()
        dp.prepare(self.con)
        data_dir.duckdb_setup(self.con)
        try:
            return trickle.read_delta(self.con, data_dir, table, self.previous_f, self.f, dp=dp)
        except FileNotFoundError as exc:
            raise MissingSourceAsset(source_pond, table) from exc

    def trickle(self, spine_ref: str, *, p: float = 0.3):
        """Start a Trickle builder on a source table.

        Args:
            spine_ref: ``"source.table"``. Any published table works. To build on one of this Pond's own
                tables, write it with a terminal earlier in the same chain.
            p: Change-fraction threshold. If this source's changes touch more than ``p`` of its rows, the
                builder recomputes in full for that run. ``1.0`` disables the check.

        Chain ``.join()``, ``.filter()``, ``.mutate()``, ``.select()``, ``.aggregate()``, ``.accumulate()`` or
        ``.sql()``, then finish with ``.merge(name, pk=...)`` or ``.append(name)``::

            (pond.trickle("orders.order_line")
                 .join(pond.trickle("catalog.product"), on="product_id")
                 .select("s0.order_id, s0.quantity, s1.unit_price")
                 .merge("priced_line", pk="order_id"))

        Reference: https://docs.duckstring.com/reference/python/trickle_builder
        """
        from .trickle_builder import TrickleBuilder

        return TrickleBuilder(self, spine_ref, p=p)


