"""SQL Ripples and static tables declared in pond.toml (plans/repositioning-features.md §2): everything
explicit, mixed freely with Python Ripples, and every table a query references checked against its
declaration."""

from __future__ import annotations

import textwrap
from pathlib import Path

import duckdb
import pytest

from duckstring.core import RippleDeclarationError, load_ripples
from duckstring.dataplane import ParquetDataPlane
from duckstring.local import load_project, run_pond


def _pond(tmp_path: Path, toml: str, files: dict[str, str] | None = None, python: str | None = None) -> Path:
    """A Pond project with Source ``src`` (a ``t`` table puddle of ids 1..3), ``toml`` appended to its
    pond.toml, the given files, and optionally a Python entrypoint."""
    (tmp_path / "pond.toml").write_text(
        '[pond]\nname = "p"\nversion = "1.0.0"\n\n[sources]\nsrc = "1.0.0"\n\n' + textwrap.dedent(toml))
    for rel, text in (files or {}).items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(textwrap.dedent(text))
    if python is not None:
        (tmp_path / "src").mkdir(exist_ok=True)
        (tmp_path / "src" / "pond.py").write_text(textwrap.dedent(python))
    data = tmp_path / "puddles" / "ponds" / "src" / "data"
    data.mkdir(parents=True, exist_ok=True)
    duckdb.sql("SELECT range AS id, range * 10 AS amount FROM range(1, 4)").write_parquet(str(data / "t.parquet"))
    return tmp_path


def _out(tmp_path: Path, table: str):
    path = ParquetDataPlane().table_path(tmp_path / "puddles" / "out", table)
    return sorted(duckdb.sql(f"SELECT * FROM read_parquet('{path}')").fetchall())


def _run(tmp_path: Path):
    result = run_pond(load_project(tmp_path))
    assert result.ok, [r.error for r in result.ripples]
    return result


# ─── running ─────────────────────────────────────────────────────────────────────


def test_a_sql_only_pond_runs(tmp_path):
    _pond(tmp_path, """
        [ripples.doubled]
        sql = "sql/doubled.sql"
        reads = ["src.t"]

        [ripples.labelled]
        sql = "sql/labelled.sql"
        parents = ["doubled"]

        [static.labels]
        path = "data/labels.csv"
    """, {
        "sql/doubled.sql": "SELECT id, amount * 2 AS amount FROM src.t",
        "sql/labelled.sql": "SELECT d.id, d.amount, l.label FROM doubled d JOIN labels l USING (id)",
        "data/labels.csv": "id,label\n1,one\n2,two\n3,three\n",
    })
    result = _run(tmp_path)
    assert [r.name for r in result.ripples] == ["doubled", "labelled"]
    assert _out(tmp_path, "doubled") == [(1, 20), (2, 40), (3, 60)]
    assert _out(tmp_path, "labelled") == [(1, 20, "one"), (2, 40, "two"), (3, 60, "three")]


def test_a_source_column_can_be_qualified_with_source_and_table(tmp_path):
    _pond(tmp_path, """
        [ripples.q]
        sql = "q.sql"
        reads = ["src.t"]
    """, {"q.sql": "WITH big AS (SELECT * FROM src.t WHERE src.t.amount > 10) SELECT big.id FROM big"})
    _run(tmp_path)
    assert _out(tmp_path, "q") == [(2,), (3,)]


def test_merge_and_append_writes(tmp_path):
    _pond(tmp_path, """
        [ripples.m]
        sql = "m.sql"
        reads = ["src.t"]
        write = "merge"
        pk = "id"

        [ripples.a]
        sql = "a.sql"
        reads = ["src.t"]
        write = "append"
        pk = ["id"]
    """, {"m.sql": "SELECT * FROM src.t", "a.sql": "SELECT * FROM src.t"})
    _run(tmp_path)
    con = duckdb.connect(str(tmp_path / "puddles" / "out" / "registry.duckdb"))
    from duckstring import trickle_io as T

    meta = T.read_meta(con)
    assert meta["m"]["mode"] == "merge" and list(meta["m"]["pk"]) == ["id"]
    assert meta["a"]["mode"] == "append"
    assert con.execute("SELECT count(*) FROM m").fetchone()[0] == 3


def test_python_and_sql_ripples_mix_in_both_directions(tmp_path):
    _pond(tmp_path, """
        [ripples.middle]
        sql = "middle.sql"
        parents = ["first"]
    """, {"middle.sql": "SELECT id + 100 AS id FROM firsts"}, python="""
        from duckstring import ripple

        @ripple
        def first(pond):
            pond.write_table("firsts", pond.con.sql("SELECT range AS id FROM range(2)"))

        @ripple(parents=["middle"])
        def last(pond):
            pond.write_table("lasts", pond.read_table("middle").project("id * 2 AS id"))
    """)
    result = _run(tmp_path)
    assert [r.name for r in result.ripples] == ["first", "middle", "last"]
    assert _out(tmp_path, "lasts") == [(200,), (202,)]


def test_static_tables_are_visible_to_python_ripples(tmp_path):
    _pond(tmp_path, """
        [static.lookup]
        path = "lookup.parquet"
    """, python="""
        from duckstring import ripple

        @ripple
        def use(pond):
            pond.write_table("used", pond.con.sql("SELECT x + 1 AS x FROM lookup"))
    """)
    duckdb.sql("SELECT 41 AS x").write_parquet(str(tmp_path / "lookup.parquet"))
    _run(tmp_path)
    assert _out(tmp_path, "used") == [(42,)]


# ─── declaration errors ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("toml, files, message", [
    ('[ripples.r]\nsql = "missing.sql"\n', {}, "doesn't exist"),
    ('[ripples.r]\nsql = "r.sql"\nwrite = "upsert"\n', {"r.sql": "SELECT 1"}, "write must be"),
    ('[ripples.r]\nsql = "r.sql"\nwrite = "merge"\n', {"r.sql": "SELECT 1"}, "needs a pk"),
    ('[ripples.r]\nsql = "r.sql"\npk = "id"\n', {"r.sql": "SELECT 1"}, "pk only applies"),
    ('[ripples.r]\nsql = "r.sql"\nreads = ["other.t"]\n', {"r.sql": "SELECT 1"}, "isn't in [sources]"),
    ('[ripples.r]\nsql = "r.sql"\nreads = ["t"]\n', {"r.sql": "SELECT 1"}, "must be source.table"),
    ('[ripples.r]\nsql = "r.sql"\nparents = ["nope"]\n', {"r.sql": "SELECT 1"}, "isn't a Ripple"),
    ('[ripples.r]\nsql = "r.sql"\nparents = ["r"]\n', {"r.sql": "SELECT 1"}, "itself as a parent"),
    ('[ripples.r]\nsql = "r.sql"\ncolour = "red"\n', {"r.sql": "SELECT 1"}, "unknown key"),
    ('[ripples.r]\nsql = "r.sql"\n', {"r.sql": "DELETE FROM x"}, "one SELECT"),
    ('[ripples.r]\nsql = "r.sql"\n', {"r.sql": "SELECT 1; SELECT 2"}, "exactly one SELECT"),
    ('[ripples.r]\nsql = "r.sql"\n[static.r]\npath = "r.csv"\n', {"r.sql": "SELECT 1", "r.csv": "a\n1\n"},
     "static table"),
    ('[static.s]\npath = "s.txt"\n', {"s.txt": "x"}, "CSV, TSV, Parquet or JSON"),
    ('[ripples.a]\nsql = "a.sql"\nparents = ["b"]\n[ripples.b]\nsql = "b.sql"\nparents = ["a"]\n',
     {"a.sql": "SELECT 1", "b.sql": "SELECT 1"}, "cycle"),
])
def test_declaration_errors(tmp_path, toml, files, message):
    _pond(tmp_path, toml, files)
    with pytest.raises(RippleDeclarationError, match=message.replace("[", r"\[").replace("]", r"\]")):
        load_ripples(tmp_path)


def test_a_python_and_a_sql_ripple_cant_share_a_name(tmp_path):
    _pond(tmp_path, '[ripples.dup]\nsql = "d.sql"\n', {"d.sql": "SELECT 1"},
          python="from duckstring import ripple\n\n@ripple\ndef dup(pond):\n    pass\n")
    with pytest.raises(RippleDeclarationError, match="same name"):
        load_ripples(tmp_path)


def test_an_unknown_python_parent_is_an_error_not_a_dropped_edge(tmp_path):
    _pond(tmp_path, "", python="""
        from duckstring import ripple

        def helper(pond):
            pass

        @ripple(parents=[helper])
        def r(pond):
            pass
    """)
    with pytest.raises(RippleDeclarationError, match="isn't a registered Ripple"):
        load_ripples(tmp_path)


# ─── the reference check ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("sql, message", [
    ("SELECT * FROM first", "isn't among its parents"),         # a sibling SQL Ripple, undeclared
    ("SELECT * FROM t", "write it as src.t"),                    # a Source table without its Source
    ("SELECT * FROM src.other", "isn't in its reads"),           # a Source table not in reads
    ("SELECT * FROM mystery", "isn't declared"),                 # nothing accounts for it
    ("SELECT * FROM x.main.t", "source.table"),                  # catalog-qualified
])
def test_undeclared_references_fail(tmp_path, sql, message):
    _pond(tmp_path, """
        [ripples.first]
        sql = "first.sql"

        [ripples.second]
        sql = "second.sql"
        reads = ["src.t"]
    """, {"first.sql": "SELECT 1 AS id", "second.sql": sql})
    with pytest.raises(RippleDeclarationError, match=message):
        load_ripples(tmp_path)


def test_declared_references_pass(tmp_path):
    _pond(tmp_path, """
        [ripples.a]
        sql = "a.sql"

        [ripples.b]
        sql = "b.sql"
        parents = ["a"]

        [ripples.c]
        sql = "c.sql"
        parents = ["b"]
        reads = ["src.t"]

        [static.s]
        path = "s.csv"
    """, {
        "a.sql": "SELECT 1 AS id",
        "b.sql": "SELECT * FROM a",
        # a grandparent, its own previous output, a static, a CTE named like a table, a table function,
        # DuckDB's metadata and a declared Source table
        "c.sql": """
            WITH mystery AS (SELECT * FROM a)
            SELECT m.id FROM mystery m, s, src.t, information_schema.schemata
            WHERE m.id NOT IN (SELECT id FROM c) AND m.id NOT IN (SELECT 1 FROM range(0))
        """,
        "s.csv": "x\n1\n",
    })
    load_ripples(tmp_path)


def test_a_python_ancestor_vouches_for_tables_it_may_write(tmp_path):
    """A Python Ripple's tables aren't known before it runs, so a SQL descendant may read a name nothing
    else declares; the run-time check covers it."""
    _pond(tmp_path, '[ripples.s]\nsql = "s.sql"\nparents = ["py"]\n', {"s.sql": "SELECT * FROM made_by_py"},
          python="from duckstring import ripple\n\n@ripple\ndef py(pond):\n    pass\n")
    load_ripples(tmp_path)


# ─── the run-time check on Python reads ──────────────────────────────────────────


def test_a_python_ripple_reading_a_non_ancestors_table_fails(tmp_path):
    _pond(tmp_path, "", python="""
        from duckstring import ripple

        @ripple
        def a_writer(pond):
            pond.write_table("shared", pond.con.sql("SELECT 1 AS x"))

        @ripple
        def b_reader(pond):  # runs after a_writer (name order), but doesn't declare it
            pond.write_table("copy", pond.read_table("shared"))
    """)
    result = run_pond(load_project(tmp_path))
    failed = [r for r in result.ripples if r.status != "ok"]
    assert [r.name for r in failed] == ["b_reader"]
    assert "RippleOrderError" in failed[0].error and "a_writer" in failed[0].error


def test_a_python_ripple_reading_a_sql_parents_table_passes(tmp_path):
    _pond(tmp_path, '[ripples.made]\nsql = "made.sql"\n', {"made.sql": "SELECT 5 AS x"}, python="""
        from duckstring import ripple

        @ripple(parents=["made"])
        def reader(pond):
            pond.write_table("copied", pond.read_table("made"))
    """)
    _run(tmp_path)
    assert _out(tmp_path, "copied") == [(5,)]


# ─── column lineage ──────────────────────────────────────────────────────────────


def _lineage(tmp_path, catalog):
    pytest.importorskip("sqlglot")
    from duckstring.sql_ripples import lineage_rows

    return {(t, c): (k, s, sc) for t, c, k, s, sc in lineage_rows(load_ripples(tmp_path), catalog)
            if k != "exact"} | {(t, c, s, sc): "exact" for t, c, k, s, sc in
                                lineage_rows(load_ripples(tmp_path), catalog) if k == "exact"}


def test_sql_ripple_column_lineage_is_exact_transitive_or_opaque(tmp_path):
    _pond(tmp_path, """
        [ripples.base]
        sql = "base.sql"
        reads = ["src.t"]

        [ripples.top]
        sql = "top.sql"
        parents = ["base", "py"]

        [static.labels]
        path = "labels.csv"
    """, {
        "base.sql": "SELECT id, amount * 2 AS doubled, 7 AS seven FROM src.t",
        "top.sql": "SELECT b.id, b.doubled, l.label, b.seven, x.whatever FROM base b JOIN labels l USING (id), "
                   "made_by_py x",
        "labels.csv": "id,label\n1,one\n",
    }, python="from duckstring import ripple\n\n@ripple\ndef py(pond):\n    pass\n")
    rows = _lineage(tmp_path, {"src.t": ["id", "amount"]})
    assert rows[("base", "doubled", "src.t", "amount")] == "exact"
    assert rows[("base", "seven")] == ("constant", "", "")
    assert rows[("top", "doubled", "src.t", "amount")] == "exact"   # through the parent SQL Ripple
    assert rows[("top", "label", "labels", "label")] == "exact"     # a static table, under its own name
    assert rows[("top", "seven")] == ("constant", "", "")
    assert rows[("top", "whatever")] == ("opaque", "", "")          # a Python parent's table: unknown


def test_sql_ripple_lineage_without_source_schemas_keeps_only_what_is_certain(tmp_path):
    _pond(tmp_path, '[ripples.r]\nsql = "r.sql"\nreads = ["src.t", "src.u"]\n',
          {"r.sql": "SELECT t.id, amount FROM src.t AS t JOIN src.u AS u USING (id)"})
    rows = _lineage(tmp_path, {})
    assert rows[("r", "id", "src.t", "id")] == "exact"   # qualified: certain without a schema
    assert rows[("r", "amount")] == ("opaque", "", "")   # unqualified over two tables, no schema: not guessed
    rows = _lineage(tmp_path, {"src.t": ["id", "amount"], "src.u": ["id"]})
    assert rows[("r", "amount", "src.t", "amount")] == "exact"  # the schemas place it


def test_a_star_over_an_unknown_table_makes_the_table_opaque(tmp_path):
    _pond(tmp_path, '[ripples.r]\nsql = "r.sql"\nreads = ["src.t"]\n', {"r.sql": "SELECT * FROM src.t"})
    assert _lineage(tmp_path, {})[("r", "")] == ("opaque", "", "")
    assert _lineage(tmp_path, {"src.t": ["id", "amount"]})[("r", "amount", "src.t", "amount")] == "exact"


def test_deploy_discovery_records_sql_ripple_lineage(tmp_path):
    pytest.importorskip("sqlglot")
    from duckstring.discover import run

    _pond(tmp_path, '[ripples.r]\nsql = "r.sql"\nreads = ["src.t"]\n', {"r.sql": "SELECT id, amount FROM src.t"})
    found = run({"source_dir": str(tmp_path), "catalog": {"src.t": ["id", "amount"]}})
    assert ["r", "amount", "exact", "src.t", "amount"] in found["lineage"]
