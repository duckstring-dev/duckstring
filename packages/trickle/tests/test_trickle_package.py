"""The package stands alone: nothing in ``trickle`` (or its tests) imports Duckstring, its host. Host
concerns reach the engine only through ``Context`` and the optional hooks it lists."""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest
import trickle
from trickle import io

PACKAGE = Path(trickle.__file__).parent
TESTS = Path(__file__).parent


def _duckstring_imports(directory: Path) -> list[str]:
    """Every import of Duckstring, or reach above the package with a relative import, in ``directory``'s
    modules. Static, so an import inside a function is caught too."""
    leaks = []
    for path in sorted(directory.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.ImportFrom):
                if node.level > 1 or (node.module or "").split(".")[0] == "duckstring":
                    leaks.append(f"{path.name}:{node.lineno} from {'.' * node.level}{node.module or ''}")
            elif isinstance(node, ast.Import):
                leaks += [f"{path.name}:{node.lineno} import {a.name}" for a in node.names
                          if a.name.split(".")[0] == "duckstring"]
    return leaks


def test_the_package_imports_nothing_from_duckstring():
    assert not _duckstring_imports(PACKAGE)


def test_the_tests_import_nothing_from_duckstring():
    assert not _duckstring_imports(TESTS)


def test_trickle_runs_as_a_top_level_package_without_duckstring():
    """Import ``trickle`` with every ``duckstring`` import blocked, import each module, run a builder join
    against a minimal host (only what ``Context`` lists), and run the README example through ``trickle.run``."""
    code = textwrap.dedent("""
        import importlib, sys
        from datetime import datetime, timezone

        class Block:
            def find_spec(self, name, path=None, target=None):
                if name == "duckstring" or name.startswith("duckstring."):
                    raise ImportError(f"blocked: {name}")

        sys.meta_path.insert(0, Block())
        import trickle
        for mod in ("context", "io", "builder", "agg", "acc", "capture", "lineage", "session"):
            importlib.import_module(f"trickle.{mod}")

        import duckdb
        from trickle import NEVER, TrickleBuilder, io

        class Host:
            def __init__(self, con, f, previous_f):
                self.con, self.f, self.previous_f = con, f, previous_f
            def read_table(self, ref):
                return io.current_state(self.con, ref)
            def read_delta(self, ref):
                return io.read_registry_delta(self.con, ref, self.previous_f, self.f, io.read_meta(self.con)[ref]["pk"])
            def merge_table(self, name, rel, *, pk, retain_t=None, retain_n=None):
                return io.merge_table(self.con, name, rel, self.f, io.normalize_pk(pk), retain_t=retain_t, retain_n=retain_n)
            def apply_zset(self, name, zset, *, pk, retain_t=None, retain_n=None):
                return io.apply_zset(self.con, name, zset, self.f, io.normalize_pk(pk), retain_t=retain_t, retain_n=retain_n)

        con = duckdb.connect()
        f1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        io.merge_table(con, "item", con.sql("SELECT 1 AS id, 'a' AS v"), f1, ("id",))
        io.merge_table(con, "price", con.sql("SELECT 1 AS id, 9 AS p"), f1, ("id",))
        host = Host(con, f1, NEVER)
        TrickleBuilder(host, "item").join(TrickleBuilder(host, "price"), on="id") \\
            .select("s0.id, s0.v, s1.p").merge("out", pk="id")
        assert io.current_state(con, "out").fetchall() == [(1, "a", 9)], io.current_state(con, "out").fetchall()
        assert io.read_meta(con)["out"]["format_version"] == io.FORMAT_VERSION

        # The README example, through the standalone host.
        from trickle import agg
        con = duckdb.connect()
        con.execute("CREATE TABLE raw_orders AS SELECT * FROM (VALUES (1, 10, 2), (2, 11, 1)) "
                    "t(order_id, product_id, quantity)")
        con.execute("CREATE TABLE raw_products AS SELECT * FROM (VALUES (10, 'Books', 12.5), (11, 'Home', 30.0)) "
                    "t(product_id, category, unit_price)")
        for _ in range(2):
            with trickle.run(con, pipeline="nightly") as t:
                t.merge_table("orders", con.sql("FROM raw_orders"), pk="order_id")
                t.merge_table("products", con.sql("FROM raw_products"), pk="product_id")
                (t.source("orders")
                   .join(t.source("products"), on="product_id")
                   .select("s1.category, s0.quantity * s1.unit_price AS revenue")
                   .aggregate(by="category", revenue=agg.sum("revenue"), orders=agg.count())
                   .merge("revenue_by_category"))
            con.execute("UPDATE raw_products SET unit_price = 20 WHERE product_id = 11")
        got = sorted(con.sql("SELECT category, revenue::DOUBLE, orders FROM revenue_by_category").fetchall())
        assert got == [("Books", 25.0, 1), ("Home", 20.0, 1)], got
        assert not [m for m in sys.modules if m == "duckstring" or m.startswith("duckstring.")]
        print("ok")
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr[-3000:]


def test_a_newer_storage_format_is_refused(db):
    f = datetime(2026, 1, 1, tzinfo=timezone.utc)
    io.merge_table(db, "m", db.sql("SELECT 1 AS id"), f, ("id",))
    assert io.read_meta(db)["m"]["format_version"] == io.FORMAT_VERSION
    db.execute(f"UPDATE {io.META_TABLE} SET format_version = {io.FORMAT_VERSION + 1}")
    with pytest.raises(io.FormatVersionError):
        io.merge_table(db, "m", db.sql("SELECT 2 AS id"), f, ("id",))
