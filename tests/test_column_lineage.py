"""Column lineage at deploy (plans/lineage.md Phase 2): captured in the discovery step, stored per version,
served by the Catchment, and never able to fail a deploy. The lineage walk itself is an engine test
(``tests/trickle/test_trickle_column_lineage.py``)."""

from __future__ import annotations

from trickle.io import first_row


def test_deploy_capture_stores_and_serves_column_lineage(tmp_path):
    from duckstring.catchment.db import connect, migrate
    from duckstring.catchment.driver import Driver
    from duckstring.catchment.launcher import NoopLauncher
    from duckstring.catchment.routes.deploy import _register, _store_column_lineage
    from duckstring.discover import capture_lineage_rows

    def priced(pond):  # a capturable Trickle ripple
        (pond.trickle("orders.order_line", p=1.0).alias("ol")
         .join(pond.trickle("prices.price", p=1.0).alias("pr"), on={"pid": "id"})
         .mutate(revenue="ol.qty * pr.unit")
         .select("ol.id AS id, revenue")
         .merge("priced", pk="id"))

    def raw(pond):  # classic read/write code — NOT capturable: absent, never guessed
        pond.write_table("t", pond.read_table("orders.order_line"))

    db = connect(tmp_path / "duck.db")
    migrate(db)
    cfg = {"sources": {"orders": "1", "prices": "1"}, "immediate_retries": 0, "source_retries": 0, "kind": "pond"}
    ripples = [{"func": priced, "name": "priced", "parents": []},
               {"func": raw, "name": "raw", "parents": []}]
    _register(db, "sales", "1.0.0", "pond", "ponds/sales/1.0.0", cfg, ripples)
    _store_column_lineage(db, "sales", "1.0.0", capture_lineage_rows(ripples, {}))

    d = Driver(db, tmp_path, "http://x", NoopLauncher())
    cols = d.lineage(pond="sales", columns=True)["ponds"][0]["columns"]
    assert "priced" in cols
    assert cols["priced"]["id"] == [{"ref": "orders.order_line", "column": "id"}]
    assert sorted((s["ref"], s["column"]) for s in cols["priced"]["revenue"]) == [
        ("orders.order_line", "qty"), ("prices.price", "unit")]
    assert "t" not in cols  # the raw ripple contributed nothing — absent, not wrong

    # recomputed wholesale on redeploy (no stale rows)
    _store_column_lineage(db, "sales", "1.0.0", capture_lineage_rows(ripples[:1], {}))
    n = first_row(db.execute("SELECT count(DISTINCT \"table\") FROM pond_version_column_lineage"))[0]
    assert n == 1


def test_deploy_capture_never_fails_the_deploy(tmp_path):
    from duckstring.catchment.db import connect, migrate
    from duckstring.catchment.routes.deploy import _register, _store_column_lineage
    from duckstring.discover import capture_lineage_rows

    def explodes(pond):
        raise RuntimeError("ripple import-time chaos")

    db = connect(tmp_path / "duck.db")
    migrate(db)
    cfg = {"sources": {}, "immediate_retries": 0, "source_retries": 0, "kind": "inlet"}
    ripples = [{"func": explodes, "name": "boom", "parents": []}]
    _register(db, "src", "1.0.0", "inlet", "ponds/src/1.0.0", cfg, ripples)
    _store_column_lineage(db, "src", "1.0.0", capture_lineage_rows(ripples, {}))  # must not raise
    assert first_row(db.execute("SELECT count(*) FROM pond_version_column_lineage"))[0] == 0


# ── Phase 3: kind:sql column lineage via sqlglot (the duckstring[lineage] extra) ──


