"""SQL Ripples and static tables, declared in ``pond.toml`` (plans/repositioning-features.md §2).

A SQL Ripple is one ``SELECT`` in a file, declared under ``[ripples.NAME]`` with everything explicit: its
``parents`` (Ripples in this Pond, SQL or Python), the Source tables it ``reads``, and how it ``write``s
the table ``NAME``. Nothing is inferred from the SQL. The SQL is only parsed (by DuckDB, with
``json_serialize_sql``) to *check* the declaration: every table the query references must be declared,
so a forgotten parent fails the deploy instead of silently reading the previous run's table.

A static table is a file shipped with the Pond's code, declared under ``[static.NAME]``, which every
Ripple sees as a read-only table ``NAME``.

Source tables are written ``source.table`` in the SQL, as in the catalog. A Source can't be registered
under that name per connection (DuckDB has no temporary schemas, and an attached catalog would be shared by
concurrent Ripples reading different pinned versions), so the parsed query has each such reference
replaced by a connection-local temporary view of the pinned version, and is turned back into SQL by DuckDB
itself (``json_deserialize_sql``). A query that reads no Source runs exactly as written.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .core import RippleDeclarationError, parse_ref

WRITE_MODES = ("overwrite", "merge", "append")
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RIPPLE_KEYS = {"sql", "parents", "reads", "write", "pk", "always_run", "cluster_by", "interleave", "cluster_bits"}
_STATIC_KEYS = {"path"}
# Schemas whose tables a query may reference without declaring them (DuckDB's own metadata).
_SYSTEM_SCHEMAS = {"information_schema", "pg_catalog"}
_STATIC_READERS = {".csv": "read_csv", ".tsv": "read_csv", ".parquet": "read_parquet",
                   ".json": "read_json", ".jsonl": "read_json", ".ndjson": "read_json"}


@dataclass
class SqlRipple:
    name: str
    path: str  # as declared, relative to the Pond's directory
    text: str
    parents: list[str] = field(default_factory=list)
    reads: list[tuple[str, str]] = field(default_factory=list)  # (source, table)
    write: str = "overwrite"
    pk: tuple[str, ...] = ()
    always_run: bool = False
    cluster: dict | None = None  # trickle.io.cluster_spec, for write = "merge"
    tree: dict | None = None  # the parsed query (json_serialize_sql), set by parse()
    refs: list[tuple[str, str, str]] = field(default_factory=list)  # (catalog, schema, table)


def _err(where: str, why: str) -> RippleDeclarationError:
    return RippleDeclarationError(f"pond.toml {where}: {why}")


def _str_list(value, where: str, key: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise _err(where, f"'{key}' must be a string or a list of strings")


def declared(source_dir: Path, info: dict) -> tuple[list[SqlRipple], dict[str, Path]]:
    """The SQL Ripples and static tables ``pond.toml`` declares, validated: names, keys, files, write
    modes and primary keys, and that each ``reads`` entry names a declared Source. Raises
    :class:`RippleDeclarationError`. The SQL itself is checked later, by :func:`check`."""
    source_dir = Path(source_dir)
    ripples_cfg = info.get("ripples") or {}
    static_cfg = info.get("static") or {}
    if not isinstance(ripples_cfg, dict) or not isinstance(static_cfg, dict):
        raise _err("[ripples]/[static]", "each entry must be a table, such as [ripples.daily_sales]")
    if (ripples_cfg or static_cfg) and (info.get("pond") or {}).get("dbt_project"):
        raise _err("[ripples]/[static]", "a dbt Pond can't also declare SQL Ripples or static tables")
    sources = set((info.get("sources") or {}).keys())

    statics: dict[str, Path] = {}
    for name, cfg in static_cfg.items():
        where = f"[static.{name}]"
        if not _NAME_RE.match(name):
            raise _err(where, "a static table's name must be letters, digits and underscores")
        if not isinstance(cfg, dict) or "path" not in cfg or not isinstance(cfg["path"], str):
            raise _err(where, "needs path = \"...\", relative to the Pond's directory")
        if set(cfg) - _STATIC_KEYS:
            raise _err(where, f"unknown key(s) {', '.join(sorted(set(cfg) - _STATIC_KEYS))}")
        path = source_dir / cfg["path"]
        if Path(cfg["path"]).suffix.lower() not in _STATIC_READERS:
            raise _err(where, f"'{cfg['path']}' isn't CSV, TSV, Parquet or JSON (by its extension)")
        if not path.is_file():
            raise _err(where, f"'{cfg['path']}' doesn't exist (or is excluded by .pondignore)")
        statics[name] = path

    out: list[SqlRipple] = []
    for name, cfg in ripples_cfg.items():
        where = f"[ripples.{name}]"
        if not _NAME_RE.match(name):
            raise _err(where, "a Ripple's name must be letters, digits and underscores")
        if name in statics:
            raise _err(where, f"'{name}' is also a static table; the two share the Pond's table names")
        if not isinstance(cfg, dict):
            raise _err(where, "must be a table")
        if set(cfg) - _RIPPLE_KEYS:
            raise _err(where, f"unknown key(s) {', '.join(sorted(set(cfg) - _RIPPLE_KEYS))}")
        if not isinstance(cfg.get("sql"), str):
            raise _err(where, "needs sql = \"...\", the path of its SQL file")
        sql_path = source_dir / cfg["sql"]
        if not sql_path.is_file():
            raise _err(where, f"'{cfg['sql']}' doesn't exist (or is excluded by .pondignore)")
        write = cfg.get("write", "overwrite")
        if write not in WRITE_MODES:
            raise _err(where, f"write must be one of {', '.join(WRITE_MODES)}")
        pk = tuple(_str_list(cfg.get("pk"), where, "pk"))
        if write == "merge" and not pk:
            raise _err(where, "write = \"merge\" needs a pk")
        if write == "overwrite" and pk:
            raise _err(where, "pk only applies to write = \"merge\" or \"append\"")
        reads: list[tuple[str, str]] = []
        for ref in _str_list(cfg.get("reads"), where, "reads"):
            try:
                src, table = parse_ref(ref)
            except ValueError as exc:
                raise _err(where, str(exc)) from None
            if src is None:
                raise _err(where, f"reads entry '{ref}' must be source.table; the Pond's own tables "
                                  "and its parents' tables aren't listed in reads")
            if src not in sources:
                raise _err(where, f"reads '{ref}', but '{src}' isn't in [sources]")
            reads.append((src, table))
        always_run = cfg.get("always_run", False)
        if not isinstance(always_run, bool):
            raise _err(where, "always_run must be true or false")
        cluster = None
        if {"cluster_by", "interleave", "cluster_bits"} & set(cfg):
            if write != "merge":
                raise _err(where, "cluster_by, interleave and cluster_bits apply only to write = \"merge\"")
            from .trickle.io import DeltaError, cluster_spec

            try:
                cluster = cluster_spec(_str_list(cfg.get("cluster_by"), where, "cluster_by"),
                                       cfg.get("interleave", True), cfg.get("cluster_bits"))
            except DeltaError as exc:
                raise _err(where, str(exc)) from None
        out.append(SqlRipple(name=name, path=cfg["sql"], text=sql_path.read_text(encoding="utf-8"),
                             parents=_str_list(cfg.get("parents"), where, "parents"), reads=reads,
                             write=write, pk=pk, always_run=always_run, cluster=cluster))
    return out, statics


# ─── parsing and the reference check ──────────────────────────────────────────


def _walk(node, visit) -> None:
    if isinstance(node, dict):
        visit(node)
        for v in node.values():
            _walk(v, visit)
    elif isinstance(node, list):
        for v in node:
            _walk(v, visit)


def parse(ripple: SqlRipple, con=None) -> None:
    """Parse the Ripple's SQL with DuckDB into ``ripple.tree`` and collect its base-table references into
    ``ripple.refs``, leaving out the query's own CTE names. Raises unless the file holds one ``SELECT``."""
    import duckdb

    con = con or duckdb.connect()
    where = f"[ripples.{ripple.name}] ({ripple.path})"
    raw = con.execute("SELECT json_serialize_sql(?)", [ripple.text]).fetchone()[0]
    tree = json.loads(raw)
    if tree.get("error"):
        raise _err(where, f"the file must hold one SELECT: {tree.get('error_message', 'unparseable')}")
    if len(tree.get("statements", [])) != 1:
        raise _err(where, "the file must hold exactly one SELECT statement")
    ctes: set[str] = set()
    refs: list[tuple[str, str, str]] = []

    def visit(n: dict) -> None:
        cte_map = n.get("cte_map")
        if isinstance(cte_map, dict):
            for entry in cte_map.get("map", []):
                if isinstance(entry, dict) and entry.get("key"):
                    ctes.add(entry["key"].lower())
        if n.get("type") == "BASE_TABLE":
            refs.append((n.get("catalog_name") or "", n.get("schema_name") or "", n.get("table_name") or ""))

    _walk(tree, visit)
    ripple.tree = tree
    ripple.refs = [r for r in refs if r[0] or r[1] or r[2].lower() not in ctes]


def check(ripple: SqlRipple, *, sql_names: set[str], statics: set[str], ancestors: set[str],
          python_ancestor: bool) -> None:
    """Every table the query references must be declared: a ``reads`` entry (written ``source.table``),
    a static table, an ancestor SQL Ripple's table, or the Ripple's own table (its previous output).

    A table no declaration accounts for is allowed only when a Python Ripple is among the ancestors, since
    a Python Ripple's tables can't be known before it runs; the Python side's own check covers it at run
    time. Raises :class:`RippleDeclarationError` naming the table and what to declare."""
    where = f"[ripples.{ripple.name}] ({ripple.path})"
    reads = {(s.lower(), t.lower()) for s, t in ripple.reads}
    by_table = {t.lower(): f"{s}.{t}" for s, t in ripple.reads}
    lower_sql = {n.lower(): n for n in sql_names}
    lower_static = {n.lower() for n in statics}
    lower_anc = {n.lower() for n in ancestors}
    for catalog, schema, table in ripple.refs:
        t = table.lower()
        if catalog:
            raise _err(where, f"references {catalog}.{schema}.{table}; write a Source table as source.table")
        if schema and schema.lower() != "main":
            if schema.lower() in _SYSTEM_SCHEMAS:
                continue
            if (schema.lower(), t) not in reads:
                raise _err(where, f"reads {schema}.{table}, which isn't in its reads")
            continue
        if t == ripple.name.lower() or t in lower_static:
            continue
        if t in lower_sql:
            if t not in lower_anc:
                raise _err(where, f"reads {lower_sql[t]}, a Ripple that isn't among its parents; add it to "
                                  "parents so it runs first")
            continue
        if t in by_table:
            raise _err(where, f"reads {table} without its Source; write it as {by_table[t]}")
        if not python_ancestor:
            raise _err(where, f"reads {table}, which isn't declared: not a parent, a Source table in reads, "
                              "or a static table")


# ─── running ─────────────────────────────────────────────────────────────────────


def _rewrite(tree: dict, views: dict[tuple[str, str], str]) -> dict:
    """A copy of ``tree`` with each ``source.table`` reference replaced by its temporary view (aliased
    as the table, unless the query gave its own alias), and columns qualified ``source.table.column``
    re-qualified by that alias."""
    tree = json.loads(json.dumps(tree))
    aliases: dict[tuple[str, str], str] = {}

    def visit(n: dict) -> None:
        if n.get("type") == "BASE_TABLE":
            key = ((n.get("schema_name") or "").lower(), (n.get("table_name") or "").lower())
            if key in views and not n.get("catalog_name"):
                aliases[key] = n.get("table_name")
                n["schema_name"] = ""
                n["table_name"] = views[key]
                if not n.get("alias"):
                    n["alias"] = aliases[key]
        elif n.get("class") == "COLUMN_REF" and isinstance(n.get("column_names"), list):
            names = n["column_names"]
            if len(names) >= 3 and (names[0].lower(), names[1].lower()) in views:
                n["column_names"] = names[1:]

    _walk(tree, visit)
    return tree


def make_callable(ripple: SqlRipple):
    """The Ripple function for a SQL Ripple: register each Source table it reads at the run's pinned
    version, run the query, and write the result as declared."""
    def run(pond):
        con = pond.con
        sql = ripple.text
        if ripple.reads:
            views = {(s.lower(), t.lower()): pond.source_view(s, t) for s, t in ripple.reads}
            tree = _rewrite(ripple.tree, views)
            sql = con.execute("SELECT json_deserialize_sql(?)", [json.dumps(tree)]).fetchone()[0]
        rel = con.sql(sql)
        if ripple.write == "merge":
            c = ripple.cluster or {}
            pond.merge_table(ripple.name, rel, pk=list(ripple.pk), cluster_by=c.get("by"),
                             interleave=c.get("interleave", True), cluster_bits=c.get("bits"))
        elif ripple.write == "append":
            pond.append_table(ripple.name, rel, pk=list(ripple.pk) or None)
        else:
            pond.write_table(ripple.name, rel)

    run.__name__ = ripple.name
    run.__qualname__ = f"sql_ripple[{ripple.name}]"
    return run


def register_statics(con, statics: dict[str, Path]) -> None:
    """Make each static table readable on ``con`` as a connection-local view over its file."""
    for name, path in statics.items():
        reader = _STATIC_READERS[path.suffix.lower()]
        literal = str(path).replace("'", "''")
        con.execute(f'CREATE OR REPLACE TEMP VIEW "{name}" AS SELECT * FROM {reader}(\'{literal}\')')


# ─── column lineage ──────────────────────────────────────────────────────────────


def _static_columns(path: Path) -> list[str]:
    import duckdb

    reader = _STATIC_READERS[path.suffix.lower()]
    literal = str(path).replace("'", "''")
    return [r[0] for r in duckdb.connect().execute(f"DESCRIBE SELECT * FROM {reader}('{literal}')").fetchall()]


def lineage_rows(pond, catalog: dict[str, list[str]]) -> list[list[str]]:
    """Static column lineage for every SQL Ripple of ``pond`` (a :class:`duckstring.core.PondRipples`), as
    the ``[table, column, kind, src_ref, src_column]`` rows deploy stores (plans/lineage.md). Resolved with
    sqlglot (the ``duckstring[lineage]`` extra; without it, nothing is recorded). Exact or absent, never
    inferred:

    - a Source column is ``(source.table, column)``. ``catalog`` holds the Sources' known columns, which
      lets ``*`` and unqualified names resolve; a column qualified with its table resolves without them
      (at a first deploy, before the Sources have run);
    - a parent SQL Ripple's column resolves to that Ripple's own provenance, transitively;
    - a static table's column is recorded under the static table's name;
    - a constant has no source columns;
    - anything sqlglot can't resolve (a Python parent's table, whose columns aren't known before it runs,
      the Ripple's own previous output, an unknown Source schema) makes the column, or the whole table,
      opaque.

    Best effort: a Ripple that fails to resolve contributes an opaque table and never fails a deploy."""
    try:
        import sqlglot
        from sqlglot import exp
        from sqlglot.lineage import lineage
        from sqlglot.optimizer.qualify import qualify
    except ImportError:
        return []

    def norm(name: str) -> str:
        return name.lower()

    schema: dict[str, dict[str, dict[str, str]]] = {"main": {}}
    originals: dict[tuple[str, str], tuple[str, dict[str, str]]] = {}  # (db, table) → (ref, {col: Col})
    for ref, cols in catalog.items():
        try:
            src, table = parse_ref(ref)
        except ValueError:
            continue
        if src is None or not cols:
            continue
        schema.setdefault(norm(src), {})[norm(table)] = {norm(c): "VARCHAR" for c in cols}
        originals[(norm(src), norm(table))] = (ref, {norm(c): c for c in cols})
    for name, path in pond.statics.items():
        try:
            cols = _static_columns(path)
        except Exception:  # noqa: BLE001 — lineage never fails a deploy
            continue
        schema["main"][norm(name)] = {norm(c): "VARCHAR" for c in cols}
        originals[("main", norm(name))] = (name, {norm(c): c for c in cols})

    source_refs = {(norm(s), norm(t)): f"{s}.{t}" for r in pond.ripples if r["kind"] == "sql"
                   for s, t in r["sql"].reads}
    own: dict[str, dict | None] = {}  # SQL Ripple → {column: provenance | None}, or None when opaque
    rows: list[list[str]] = []
    for name in pond.order():
        r = pond.by_name[name]
        if r["kind"] != "sql":
            continue
        result = None
        known = {db: tables for db, tables in schema.items() if tables}  # sqlglot rejects an empty database
        try:
            tree = qualify(sqlglot.parse_one(r["sql"].text, dialect="duckdb"), schema=known, db="main",
                           dialect="duckdb", validate_qualify_columns=False)
            names = [p.alias_or_name for p in tree.selects]
            if "*" in names:
                raise ValueError("a * over a table with unknown columns")  # the output columns aren't known
            result = {}
            for col in names:
                if col in result:
                    result[col] = None  # a repeated output name: ambiguous, so opaque
                    continue
                prov: set | None = set()
                stack = [lineage(col, tree, schema=known, dialect="duckdb")]
                while stack and prov is not None:
                    node = stack.pop()
                    if node.downstream:
                        stack.extend(node.downstream)
                        continue
                    if isinstance(node.source, exp.Select) and not isinstance(node.expression, exp.Column):
                        continue  # a constant or count(*): no source column
                    if not isinstance(node.source, exp.Table):
                        prov = None  # a column sqlglot couldn't place (no schema to resolve it against)
                        continue
                    db, table = norm(node.source.db or "main"), norm(node.source.name)
                    leaf = norm(exp.to_column(node.name).name)
                    if leaf == "*":
                        prov = None
                    elif db == "main" and table in own:  # a parent SQL Ripple: its provenance, transitively
                        parent = own[table]
                        p = None if parent is None else parent.get(leaf)
                        prov = None if p is None else prov | p
                    elif (db, table) in originals:
                        ref, cols = originals[(db, table)]
                        prov.add((ref, cols.get(leaf, leaf)))
                    elif db != "main" and (db, table) in source_refs:  # a Source with no known schema yet
                        prov.add((source_refs[(db, table)], leaf))
                    else:
                        prov = None
                result[col] = prov
        except Exception:  # noqa: BLE001 — unresolvable (an unknown table or column): opaque
            result = None
        own[norm(name)] = None if result is None else {norm(c): p for c, p in result.items()}
        if result is None:
            rows.append([name, "", "opaque", "", ""])
            continue
        schema["main"][norm(name)] = {norm(c): "VARCHAR" for c in result}
        for col, prov in result.items():
            if prov is None:
                rows.append([name, col, "opaque", "", ""])
            elif not prov:
                rows.append([name, col, "constant", "", ""])
            else:
                rows.extend([name, col, "exact", ref, sc] for ref, sc in sorted(prov))
    return rows
