from __future__ import annotations

import time
from pathlib import Path

_RIPPLES: list[dict] = []
_PUDDLES: list[dict] = []


class RippleDeclarationError(ValueError):
    """A Pond's Ripples are declared inconsistently: an unknown or cyclic parent, a duplicate name, an
    invalid ``[ripples]`` or ``[static]`` entry in ``pond.toml``, or a SQL Ripple reading a table it
    doesn't declare. Raised at deploy (a 422) and on a local run. A subclass of ``ValueError``."""


class RippleOrderError(RuntimeError):
    """A Ripple read a table that another Ripple in the Pond writes, without that Ripple among its
    parents, so the read could see the previous run's data. Fails the Ripple."""


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
            references or names (a name also reaches a SQL Ripple declared in ``pond.toml``).
            Dependencies on other Ponds are declared in ``pond.toml``.
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


class PondRipples:
    """Every Ripple of a Pond, Python and SQL, with parents resolved to names (see :func:`load_ripples`).

    ``ripples`` rows are ``{"name", "func", "parents": [names], "always_run", "kind": "python"|"sql"}``;
    ``statics`` maps each static table's name to its file."""

    def __init__(self, ripples: list[dict], statics: dict[str, Path]):
        self.ripples = ripples
        self.statics = statics
        self.by_name = {r["name"]: r for r in ripples}
        self._ancestors: dict[str, set[str]] = {}

    def topology(self) -> dict[str, list[str]]:
        return {r["name"]: list(r["parents"]) for r in self.ripples}

    def ancestors(self, name: str) -> set[str]:
        if name not in self._ancestors:
            seen: set[str] = set()
            stack = list(self.by_name[name]["parents"])
            while stack:
                p = stack.pop()
                if p not in seen:
                    seen.add(p)
                    stack.extend(self.by_name[p]["parents"])
            self._ancestors[name] = seen
        return self._ancestors[name]

    def sql_writers(self) -> dict[str, str]:
        """``{table: ripple}`` for the tables known before anything runs: each SQL Ripple's own."""
        return {r["name"]: r["name"] for r in self.ripples if r["kind"] == "sql"}

    def order(self) -> list[str]:
        """The Ripple names in a dependency order (parents first; ties by name)."""
        out: list[str] = []
        done: set[str] = set()
        remaining = self.topology()
        while remaining:
            ready = sorted(n for n, ps in remaining.items() if all(p in done for p in ps))
            if not ready:
                break  # a cycle: the rest can never run (load_ripples reports it)
            for n in ready:
                out.append(n)
                done.add(n)
                remaining.pop(n)
        return out


def load_ripples(source_dir: Path, info: dict | None = None, *, require_entry: bool = False) -> PondRipples:
    """Every Ripple of the Pond at ``source_dir``: the ``@ripple`` functions in its Python entrypoint (when
    it has one) and the SQL Ripples and static tables its ``pond.toml`` declares, with every parent
    resolved to a Ripple name and each SQL Ripple's query checked against its declaration
    (:mod:`duckstring.sql_ripples`). The one place a Pond's Ripples are collected: deploy discovery, the
    Duck and the local runner all use it, so they agree. Raises :class:`RippleDeclarationError` on an
    unknown parent, a duplicate name or a cycle; with ``require_entry``, also when there's no Python
    entrypoint and no SQL Ripples."""
    from . import sql_ripples

    source_dir = Path(source_dir)
    info = read_pond_toml(source_dir) if info is None else info
    entry, _ = pond_entrypoints(info)
    raw: list[dict] = []
    if (source_dir / entry).exists():
        try:
            import_pond_module(source_dir, entry)
        finally:
            raw = collect_ripples()
    sql, statics = sql_ripples.declared(source_dir, info)
    if require_entry and not raw and not sql and not (source_dir / entry).exists():
        raise FileNotFoundError(f"no ripples entrypoint at {entry} and no [ripples] in pond.toml — "
                                "is this a Pond project?")

    func_to_name = {r["func"]: r["name"] for r in raw}
    rows: list[dict] = []
    names: set[str] = set()
    for r in raw:
        if r["name"] in names:
            raise RippleDeclarationError(f"two Ripples are named '{r['name']}'")
        names.add(r["name"])
    for r in sql:
        if r.name in names:
            raise RippleDeclarationError(f"pond.toml [ripples.{r.name}]: a Python Ripple has the same name")
        if r.name in statics:
            raise RippleDeclarationError(f"pond.toml [ripples.{r.name}]: a static table has the same name")
        names.add(r.name)
    for r in raw:
        if r["name"] in statics:
            raise RippleDeclarationError(f"Ripple '{r['name']}' has the same name as a static table")

    def resolve(owner: str, p) -> str:
        if isinstance(p, str):
            name = p
        elif p in func_to_name:
            name = func_to_name[p]
        else:
            raise RippleDeclarationError(
                f"Ripple '{owner}' has a parent that isn't a registered Ripple: {getattr(p, '__name__', p)!r}")
        if name not in names:
            raise RippleDeclarationError(f"Ripple '{owner}' has parent '{name}', which isn't a Ripple in this Pond")
        if name == owner:
            raise RippleDeclarationError(f"Ripple '{owner}' lists itself as a parent")
        return name

    for r in raw:
        rows.append({"name": r["name"], "func": r["func"], "always_run": bool(r.get("always_run")),
                     "parents": [resolve(r["name"], p) for p in r["parents"]], "kind": "python"})
    for r in sql:
        rows.append({"name": r.name, "func": sql_ripples.make_callable(r), "always_run": r.always_run,
                     "parents": [resolve(r.name, p) for p in r.parents], "kind": "sql", "sql": r})
    pond = PondRipples(rows, statics)
    if len(pond.order()) != len(rows):
        stuck = sorted(set(pond.by_name) - set(pond.order()))
        raise RippleDeclarationError(f"the Ripples' parents form a cycle: {', '.join(stuck)}")

    if sql:
        import duckdb

        con = duckdb.connect()
        try:
            sql_names = {r.name for r in sql}
            for r in sql:
                sql_ripples.parse(r, con)
                ancestors = pond.ancestors(r.name)
                sql_ripples.check(r, sql_names=sql_names, statics=set(statics), ancestors=ancestors,
                                  python_ancestor=any(pond.by_name[a]["kind"] == "python" for a in ancestors))
        finally:
            con.close()
    return pond


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
    code never sees stale state. The Ripple and Puddle registries are cleared first, so collecting
    afterwards returns only what this import registered."""
    import importlib
    import sys

    _RIPPLES.clear()
    _PUDDLES.clear()
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

    Results are DuckDB relations loaded on ``con``. Queries target the Pond's highest deployed major, or with a
    Tap's key, the Tap's.

    Args:
        url: The Catchment's address.
        con: DuckDB connection for results. Defaults to a new in-memory connection.
        default_pond: Pond used when a method is called without ``pond``.
        default_table: Table used when :meth:`get` is called without ``table``.
        api_key: Sent as ``Authorization: Bearer <key>`` unless ``headers`` sets ``Authorization``. With a
            Tap's key, ``pond`` can be left out everywhere, since the key implies it.
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
        self._tap = bool(api_key and api_key.startswith("tap_"))  # a Tap's key implies its Pond

    @property
    def con(self):
        if self._con is None:
            import duckdb

            self._con = duckdb.connect()
        return self._con

    def _pond(self, pond: str | None) -> str | None:
        target = pond or self._default_pond
        if not target and not self._tap:
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
        when the Catchment returns an error, and ``ValueError`` when no Pond is given (unless the key is a
        Tap's).
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


def _parse_f(iso: str):
    from datetime import datetime

    return datetime.fromisoformat(iso)


def _is_relation(value) -> bool:
    import duckdb

    return isinstance(value, duckdb.DuckDBPyRelation)


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
        flock: str | None = None, sources=None, flock_env: dict[str, str] | None = None,
        static_tables: dict | None = None, ripple: str | None = None, ancestors=None,
        table_writers: dict[str, str] | None = None, change_sink=None,
    ) -> None:
        from .changes import ChangeLedger
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
        # Per-object change detection (plans/change-detection.md): the run's change ledger, which records
        # what this Ripple wrote, checked and held. The runtime passes the Run's; a handle made without one
        # (a test, a standalone call) keeps its own, so own-object ``changed`` still works.
        self._changes = change_sink if change_sink is not None else ChangeLedger().begin(f, ripple or "")
        self._checks: dict[str, tuple] = {}  # name → (relation, fingerprint) from the last detect_change
        self._own_sidecar_cache: dict | None = None
        # Non-tabular Objects (see objects.py / plans/objects.md). ``staging_dir`` is the local dir a
        # ``write_object`` stages into (committed at export by the runtime); ``own_data_dir`` is where this
        # Pond's own Objects are published (for own reads). Both set by the runtime; None outside a run.
        self._staging_dir = staging_dir
        self._own_data_dir = own_data_dir
        self._object_scratch = None
        # The Pond's resolved Flock posture (off|upgrade|always) and the run's Flock settings (an environment
        # mapping: the begin_run job's over the Duck's own; None reads the process environment), threaded
        # in by the runtime; the trickle terminals read both.
        self.flock = flock
        self.flock_env = flock_env
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
        # The freshness the Catchment says each Source has published when this run started. It pins
        # overwrite Source reads to that version (see _pin) and lets a read reject a stale local publish
        # (catchment.registry.resolve_data_dir). Absent for puddle runs.
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
        # Which Ripple this handle serves, its ancestors, and which Ripple writes each of the Pond's tables
        # (as far as known), for the check that an own-table read only sees an ancestor's output. Set by
        # the runtime; None outside a run, which disables the check.
        self._ripple = ripple
        self._ancestors = set(ancestors or ())
        self._table_writers = table_writers
        # Static tables declared in pond.toml ([static.NAME]): connection-local views over the shipped files.
        if static_tables:
            from .sql_ripples import register_statics

            register_statics(con, static_tables)

    def _check_own_read(self, table: str) -> None:
        """Fail the read of one of this Pond's tables written by a Ripple that isn't this one or an
        ancestor: that Ripple may not have run yet, so the read could see the previous run's data."""
        if self._ripple is None or self._table_writers is None:
            return
        writer = self._table_writers.get(table)
        if writer is None or writer == self._ripple or writer in self._ancestors:
            return
        raise RippleOrderError(
            f"Ripple '{self._ripple}' reads '{table}', which Ripple '{writer}' writes, but '{writer}' isn't "
            f"among its parents. Add it to parents so it runs first."
        )

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

        Downstream Ponds then treat this Pond's output as unchanged, and a Pond whose Sources are all
        unchanged passes without running. Freshness still advances. Calling it from any Ripple marks the
        whole run, so call it once you know nothing the Pond publishes changed, usually from a Ripple that
        runs last (see :meth:`changed`). Has no effect in a local run.
        """
        if self._skip_sink is not None:
            self._skip_sink()

    # ─── change detection (plans/change-detection.md) ──────────────────────────

    def changed(self, *refs: str) -> bool:
        """Whether any of ``refs`` (``"name"`` or ``"source.name"``) changed, as seen by this run. With no
        arguments, whether this run has changed any of the Pond's own tables or Objects so far.

        - A Source's plain table: the version this run reads was published after the previous run.
        - A Source's append or merge Trickle: rows were added, changed or removed in the window
          ``(previous_f, f]``, or the window can't be read as changes (the Pond's first run, or
          ``previous_f`` older than the Source's retained history).
        - A Source's Object: it was written after the previous run.
        - One of this Pond's own tables or Objects: this run wrote it and didn't keep the published copy. A
          Trickle write counts only if it changed the table.

        The own forms only see writes made through the handle (``write_table``, ``write_object``,
        ``append_table``, ``merge_table``, ``apply_zset`` and the builder's terminals) and checks made with
        :meth:`detect_change`. A table changed with SQL on :attr:`con` doesn't count. A Source is compared
        with the previous run's freshness, so when Sources share an upstream it can occasionally report
        ``True`` for a change the previous run already read: a redundant recompute, never a missed change.

        Raises :class:`RippleOrderError` for one of the Pond's own tables written by a Ripple that isn't
        this Ripple or one of its ancestors, and :class:`MissingSourceAsset` for a Source table or Object
        that hasn't been published.

        Example, in a Ripple that runs after the others::

            if not pond.changed():
                pond.skip()

        Reference: https://docs.duckstring.com/reference/python/pond#changed
        """
        if not refs:
            return self._changes.any_changed()
        for ref in refs:
            source, name = self._resolve_ref(ref)
            if source is None:
                self._check_own_read(name)
                if self._changes.changed(name):
                    return True
            elif self._source_changed(source, name):
                return True
        return False

    def _source_changed(self, source: str, name: str) -> bool:
        from . import trickle_io as trickle
        from .dataplane import get_data_plane
        from .engine.core import NEVER

        if self.previous_f == NEVER:
            return True
        data_dir = self._source_data_dir(source)
        sidecar = trickle.load_sidecar(data_dir)
        entry = trickle.sidecar_tables(sidecar).get(name)
        if entry is None:
            obj = (sidecar.get("objects") or {}).get(name)
            if obj is not None:
                return not obj.get("f") or _parse_f(obj["f"]) > self.previous_f
            try:  # a table published without a sidecar entry (a Puddle snapshot): its age is unknown
                get_data_plane().read_select(data_dir, name)
            except FileNotFoundError as exc:
                raise MissingSourceAsset(source, name) from exc
            return True
        if entry.get("mode", "overwrite") == "overwrite":
            pin = self._pin(source)
            version_f = trickle.overwrite_version_f(data_dir, name, pin if pin is not None else self.f)
            if version_f is None:
                version_f = _parse_f(entry["f"]) if entry.get("f") else None
            return version_f is None or version_f > self.previous_f
        data_dir.duckdb_setup(self.con)
        return trickle.window_changed(self.con, data_dir, name, self.previous_f, self.f)

    def detect_change(self, name: str, value, *, exact: bool = False) -> bool:
        """Whether ``value`` differs from what is published as this Pond's own table or Object ``name``.
        Nothing is written.

        ``value`` is a ``duckdb.DuckDBPyRelation``, compared as a table, or a path, bytes or a binary
        file-like, compared as an Object (a file-like is read, then rewound when it can be). A table is
        compared by fingerprint: a hash over every row, independent of row order and counting duplicate rows,
        combined with the column names and types. It costs one scan of ``value``. With ``exact=True``
        (tables only) the rows themselves are compared with the Pond's copy of the table (``EXCEPT ALL`` in
        both directions), which is slower but can't be fooled by a hash collision. An Object is compared by
        a SHA-256 of its payload, with a directory hashed file by file.

        Returns ``True`` whenever it can't show the two are the same: nothing published yet, a table last
        written without a fingerprint, a fingerprint taken by a different DuckDB version, or a Pond database
        that no longer holds the published table (a failed run left newer data there).

        If it returns ``False`` and the run doesn't then write ``name``, the published copy is kept. Writing
        anyway is always allowed and counts as a change. ``write_table(..., if_changed=True)`` and
        ``write_object(..., if_changed=True)`` are shorthands for checking and then writing, and a
        ``write_table`` of the same relation object right after a check records its fingerprint.

        Reference: https://docs.duckstring.com/reference/python/pond#detect_change
        """
        source, name = self._resolve_ref(name, "table or Object")
        if source is not None:
            raise ValueError(f"detect_change compares this Pond's own output; got a Source reference '{source}.{name}'")
        if _is_relation(value):
            unchanged = not self._table_changed(name, value, exact)
        else:
            from .objects import object_digest

            entry = (self._own_sidecar().get("objects") or {}).get(name)
            unchanged = entry is not None and entry.get("sha256") == object_digest(value)
            if unchanged and not self._changes.written(name) and self._staging_dir is not None:
                from .objects import unstage_object

                unstage_object(Path(self._staging_dir), name)  # a payload a failed run left staged
        self._changes.record_check(name, unchanged)
        return not unchanged

    def _table_changed(self, name: str, relation, exact: bool) -> bool:
        from .dataplane import fingerprint, registry_fingerprints

        fp = fingerprint(relation)
        self._checks[name] = (relation, fp)
        from .trickle_io import sidecar_tables

        published = sidecar_tables(self._own_sidecar()).get(name) or {}
        baseline = published.get("fp") if published.get("mode", "overwrite") == "overwrite" else None
        # The Pond database must hold exactly the published table too, because later Ripples read it there.
        if baseline is None or registry_fingerprints(self.con).get(name) != baseline:
            return True
        if exact:
            return not self._same_rows(name, relation)
        return fp != baseline

    def _same_rows(self, name: str, relation) -> bool:
        """Whether ``relation`` holds exactly the rows of the Pond's table ``name`` (as a multiset)."""
        q = name.replace('"', '""')
        try:
            current = self.con.sql(f'SELECT * FROM "{q}" LIMIT 0')
            if list(current.columns) != list(relation.columns) or \
                    [str(t) for t in current.types] != [str(t) for t in relation.types]:
                return False
            v = "_duckstring_detect_new"
            (diff,) = relation.query(v, f'SELECT count(*) FROM ((SELECT * FROM {v} EXCEPT ALL SELECT * FROM "{q}") '
                                        f'UNION ALL (SELECT * FROM "{q}" EXCEPT ALL SELECT * FROM {v}))').fetchone()
        except Exception:  # not comparable here (a relation on another connection): treat as changed
            return False
        return diff == 0

    def _own_sidecar(self) -> dict:
        """This Pond's published sidecar, read once per handle (``{}`` outside a run)."""
        if self._own_sidecar_cache is None:
            from .trickle_io import load_sidecar

            self._own_sidecar_cache = load_sidecar(self._own_store()) if self._own_data_dir is not None else {}
        return self._own_sidecar_cache

    def record_change(self, name: str, changed: bool) -> None:
        """Record that this run wrote ``name`` and whether the write changed it: the optional host hook the
        Trickle builder's terminals call, as for :meth:`record_lineage_write`."""
        self._changes.record_write(name, changed)

    def dispatch_comprehensive(self, job):
        """The Trickle builder's comprehensive-dispatch hook: offer a terminal's comprehensive recompute (a
        :class:`trickle.ComprehensiveJob`) to the Flock (:mod:`duckstring.flock`), under this Pond's posture
        and the run's Flock settings. ``None`` (the common case: no engine configured, or the mode ladder
        keeps it local) runs it locally."""
        from . import flock

        if not flock.enabled(self.flock_env):
            return None
        return flock.comprehensive(job, pond_mode=self.flock, env=self.flock_env)

    def write_table(self, name: str, relation, *, if_changed: bool = False, cluster_by=None, interleave=True,
                    cluster_bits=None) -> bool:
        """Replace the table ``name`` in the Pond's database with ``relation``, in one transaction.

        Every table in the database is published when the whole Pond Run succeeds; if any Ripple fails,
        nothing from the run is published. A write that collides with another Ripple's is retried. Tables whose
        names start with ``_duckstring_`` are never published, and columns with that prefix are rejected at
        publish. Use :meth:`append_table` or :meth:`merge_table` to keep history for incremental consumers.

        With ``if_changed=True`` the table is written only if :meth:`detect_change` finds that ``relation``
        differs from the published table. When it doesn't, nothing is written and the published table is
        kept as it is.

        ``cluster_by``, ``interleave`` and ``cluster_bits`` order the table as it's written, as for
        :meth:`merge_table`, so reads filtering on those columns skip most of it. The table is sorted on every
        write; without ``cluster_by`` it keeps the order ``relation`` produced.

        Returns ``True`` if the table was written. Without ``if_changed`` it always is.
        """
        from . import trickle_io as trickle
        from .dataplane import FP_TABLE, _ensure_fp_table, set_registry_fingerprint

        if if_changed and not self.detect_change(name, relation):
            return False
        # A fingerprint taken of this same relation lets the next run's check compare with this write.
        check = self._checks.pop(name, None)
        fp = check[1] if check is not None and check[0] is relation else None
        spec = trickle.cluster_spec(cluster_by, interleave, cluster_bits)
        if spec is not None:
            from .dataplane import cluster_order_select

            missing = [c for c in spec["by"] if c not in relation.columns]
            if missing:
                raise trickle.DeltaError(f"cluster_by column(s) {missing} not in '{name}'")
            relation = self.con.sql(cluster_order_select(self.con, f"({relation.sql_query()})", spec))
        self.record_lineage_write(name)
        tmp = f"__tmp_{name}"
        # The registry fingerprint is set by a fingerprinted write and cleared by any other, so it never
        # describes content it wasn't taken from.
        track_fp = fp is not None or trickle._table_exists(self.con, FP_TABLE)
        if fp is not None:
            retry_on_lock(lambda: _ensure_fp_table(self.con))

        def _write() -> None:
            self.con.execute("BEGIN TRANSACTION")
            try:
                self.con.execute(f'DROP TABLE IF EXISTS "{tmp}"')
                relation.create(f'"{tmp}"')
                self.con.execute(f'DROP TABLE IF EXISTS "{name}"')
                self.con.execute(f'ALTER TABLE "{tmp}" RENAME TO "{name}"')
                if track_fp:
                    set_registry_fingerprint(self.con, name, fp, self.f)
                self.con.execute("COMMIT")
            except Exception:
                self.con.execute("ROLLBACK")  # release the txn so a retry starts clean
                raise

        retry_on_lock(_write)  # a concurrent write conflict queues + retries rather than failing
        self._changes.record_write(name, True)
        return True

    def write_object(self, name: str, src, *, if_changed: bool = False) -> bool:
        """Stage a non-tabular Object (a model, a file, a directory) to be published with the run's tables.

        ``name`` is letters, digits and underscores, optionally with single dots between parts
        (``model.pkl``); read a dotted name back with backticks, ``read_object("`model.pkl`")``.

        ``src`` is a file or directory path, ``bytes``, or a binary file-like. The Object is replaced as one
        unit when the run publishes; a later Ripple failure leaves the previous Object in place.

        With ``if_changed=True`` the Object is published only if its content differs from the published
        Object, compared by a SHA-256 of the payload. When it doesn't, the published Object is kept, with
        the freshness of the run that last changed it.

        Returns ``True`` if the Object was staged for publishing; without ``if_changed`` it always is. Raises
        ``RuntimeError`` outside a Pond Run.
        """
        from .objects import stage_object, unstage_object

        if self._staging_dir is None:
            raise RuntimeError("write_object is only available inside a Pond Run (no staging context)")
        digest = stage_object(Path(self._staging_dir), name, src)
        if if_changed:
            entry = (self._own_sidecar().get("objects") or {}).get(name)
            if entry is not None and entry.get("sha256") == digest:
                unstage_object(Path(self._staging_dir), name)
                self._changes.record_check(name, True)
                return False
        self.record_lineage_write(name)
        self._changes.record_write(name, True)
        return True

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

    def _pin(self, source_pond: str):
        """The Source's published freshness when this run started (from the ``begin_run`` job), which
        selects the version of its overwrite tables every Ripple of the run reads. ``None`` without a job
        (a puddle run), where reads fall back to this run's own freshness."""
        from datetime import datetime

        iso = self.source_f.get(source_pond)
        return datetime.fromisoformat(iso) if iso else None

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
        (``FROM product``), unless one of this Pond's own tables has that name. Source reads are pinned to the run:
        a plain table is read at the version the Source had published when the run started, and a Trickle up to
        the run's freshness, so every Ripple in a run sees the same Source data.
        For a Trickle, the result is the current state without the ``_duckstring_*`` system columns.

        Refer to the registered view name in SQL rather than to a Python variable holding the relation, which
        is unreliable under the Duck's threaded executor.

        Raises :class:`MissingSourceAsset` when a Source table isn't published, and :class:`RippleOrderError`
        when reading one of the Pond's own tables that another Ripple writes, if that Ripple isn't among this
        Ripple's parents (directly or further up).
        """
        source_pond, table = self._resolve_ref(ref)
        if source_pond is not None:
            rel = self._source_relation(source_pond, table)
            from .trickle_io import _is_current_view

            if not _is_current_view(self.con, table):  # never replace this Pond's own merge Trickle view
                try:
                    rel.create_view(table, replace=True)
                except Exception:
                    pass  # name taken by one of this Pond's own tables — the relation still works
            return rel
        self._check_own_read(table)
        self._record_read(None, table)
        return self._own_current(table)

    def _source_relation(self, source_pond: str, table: str):
        """A Source table at this run's pinned version, without system columns (recorded as a read).

        Pinned reads: a Pond Run spans several Ripples and a Source can republish mid-run, so an overwrite
        table is read at the version the Source had published when this run started (`_pin`), and a
        Trickle table up to this run's freshness. Every Ripple of the run sees the same data, and the
        Source keeps the pinned version until the run is done."""
        from .dataplane import get_data_plane
        from .trickle_io import _strip_system

        self._record_read(source_pond, table)
        data_dir = self._source_data_dir(source_pond)
        dp = get_data_plane()
        data_dir.duckdb_setup(self.con)  # object store → httpfs + credentials (no-op for local)
        try:
            select = dp.read_select(data_dir, table, as_of=self.f, pin=self._pin(source_pond))
        except FileNotFoundError as exc:
            raise MissingSourceAsset(source_pond, table) from exc
        return _strip_system(self.con.sql(select))

    def source_view(self, source: str, table: str) -> str:
        """Register a Source table at this run's pinned version as a view local to this connection and
        return its name. How a SQL Ripple reads ``source.table`` (see :mod:`duckstring.sql_ripples`)."""
        if self._declared_sources is not None and source not in self._declared_sources:
            raise ValueError(f"'{source}' is not a declared Source of this Pond")
        rel = self._source_relation(source, table)
        name = f"_duckstring_src__{source}__{table}"
        quoted = name.replace('"', '""')
        self.con.execute(f'CREATE OR REPLACE TEMP VIEW "{quoted}" AS {rel.sql_query()}')
        return name

    def flock_source_select(self, ref: str, columns, *, object_store_only: bool = True) -> str | None:
        """A query over a Source table's published copy at this run's pinned version, for an engine that
        reads the data root itself (the Flock), or ``None`` when it should be sent the data instead: an own
        table, a puddle run, or a data root the engine can't reach. ``columns`` are the user columns, as
        :meth:`read_table` gives them.

        A Source co-located on this machine is read locally (:meth:`_source_data_dir`) and is mirrored to
        the data root after it publishes, so the data root's copy is used only when its sidecar shows it has
        caught up with the version this run reads."""
        from .catchment.registry import local_publish_f, pond_data_dir
        from .dataplane import get_data_plane
        from .storage import is_object_uri
        from .trickle_io import _q

        source, table = self._resolve_ref(ref)
        if source is None:
            return None
        major = self.source_majors.get(source)
        if major is None or self.data_root is None or not is_object_uri(self.data_root):
            if object_store_only:  # a puddle run or a local data root: only this machine can read it
                return None
            data_dir = self._source_data_dir(source)
        else:
            data_dir = pond_data_dir(Path(self.root), source, major, self.data_root)
            expected = self.source_f.get(source)
            if expected is not None and (local_publish_f(data_dir) or "") < expected:
                return None
        try:
            select = get_data_plane().read_select(data_dir, table, as_of=self.f, pin=self._pin(source))
        except FileNotFoundError:
            return None
        cols = ", ".join(_q(c) for c in columns)
        return f"SELECT {cols} FROM ({select}) AS {_q('_duckstring_leaf')}"

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
            data_dir.duckdb_setup(self.con)
            meta = trickle.load_sidecar(data_dir).get(table, {})
            (n,) = self.con.execute(
                dp.consolidated_count_select(data_dir, table, meta, as_of=self.f, pin=self._pin(source_pond))
            ).fetchone()
            return int(n)
        self._check_own_read(table)
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
        changed = trickle.append_table(
            self.con, name, relation, self.f, self._resolve_pk(pk),
            fail_on_conflict=fail_on_conflict, retain_t=retain_t, retain_n=retain_n,
        )
        self._changes.record_write(name, changed)
        return changed

    def merge_table(self, name: str, relation, *, pk, retain_t=None, retain_n=None,
                    compact_threshold=None, cluster_by=None, interleave=True, cluster_bits=None) -> bool:
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
            cluster_by: A column or columns to order the table's compacted base by, so reads filtering on
                them skip most of it. Defaults to ordering by ``pk``. Takes effect at the next compaction.
            interleave: With two or more ``cluster_by`` columns, interleave them (a rank-Hilbert order, so
                each column is clustered about equally) rather than sort by them in order. Default ``True``.
            cluster_bits: The interleaved order's precision: it splits the data into ``2**cluster_bits``
                cells, rounded up to a multiple of the column count. Defaults to a size chosen at each
                compaction, about one row group per cell.

        Returns ``True`` if the state changed, the usual signal for :meth:`skip`. Raises ``DeltaError`` for a
        missing or empty ``pk``, or an invalid clustering.

        Within the Pond, ``name`` is a view over the current state (no system columns), so later Ripples can
        query it in SQL; the compacted base is ``{name}__base``.

        Reference: https://docs.duckstring.com/reference/python/trickle_io
        """
        from . import trickle_io as trickle

        self.record_lineage_write(name)
        changed = trickle.merge_table(
            self.con, name, relation, self.f, self._resolve_pk(pk),
            retain_t=retain_t, retain_n=retain_n, compact_threshold=compact_threshold,
            cluster_by=cluster_by, interleave=interleave, cluster_bits=cluster_bits,
        )
        self._changes.record_write(name, changed)
        return changed

    def apply_zset(self, name: str, zset, *, pk, retain_t=None, retain_n=None,
                   compact_threshold=None, cluster_by=None, interleave=True, cluster_bits=None) -> bool:
        """Append an already-computed change to the merge Trickle ``name`` without comparing it with the state.

        ``zset`` holds the table's columns plus ``_duckstring_d`` (``+1`` added, ``-1`` removed; an update is a
        ``-1`` of the old row and a ``+1`` of the new). It is consolidated before writing. ``pk`` is required;
        ``retain_t``, ``retain_n``, ``compact_threshold``, ``cluster_by``, ``interleave`` and ``cluster_bits``
        are as for :meth:`merge_table`. Returns ``True`` if the consolidated change is non-empty.

        This is the write the Trickle builder uses. Prefer :meth:`trickle` or :meth:`merge_table`: a wrong
        weight corrupts the table for every consumer.
        """
        from . import trickle_io as trickle

        self.record_lineage_write(name)
        changed = trickle.apply_zset(
            self.con, name, zset, self.f, self._resolve_pk(pk),
            retain_t=retain_t, retain_n=retain_n, compact_threshold=compact_threshold,
            cluster_by=cluster_by, interleave=interleave, cluster_bits=cluster_bits,
        )
        self._changes.record_write(name, changed)
        return changed

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
        data_dir.duckdb_setup(self.con)
        try:
            return trickle.read_delta(self.con, data_dir, table, self.previous_f, self.f, dp=dp,
                                      pin=self._pin(source_pond))
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


