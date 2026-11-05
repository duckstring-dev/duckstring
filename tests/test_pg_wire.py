"""Data serving increment 3: the Postgres wire adapter. Driven with a real pg client (pg8000, which
uses the EXTENDED protocol like BI tools/JDBC), so a green run validates the wire — startup/auth, the
extended Parse/Bind/Describe/Execute flow, results, and DuckDB answering catalog introspection."""

from __future__ import annotations

import duckdb
import pytest
from trickle.io import first_row

from duckstring.catchment.db import connect, migrate
from duckstring.catchment.driver import Driver
from duckstring.catchment.launcher import NoopLauncher
from duckstring.catchment.pg_wire import PgWireServer
from duckstring.catchment.registry import pond_data_dir
from duckstring.catchment.routes.deploy import _register

pg8000 = pytest.importorskip("pg8000.native")

pytestmark = pytest.mark.timeout(15)

_RIPPLES = [{"func": "f", "name": "r", "parents": []}]


def _cfg(serve_tables):
    return {"sources": {}, "immediate_retries": 0, "source_retries": 0, "kind": "outlet",
            "serve_tables": serve_tables}


def _pv(db, name, version):
    return first_row(db.execute("SELECT pv.id FROM pond_version pv JOIN pond_name pn ON pn.id = pv.pond_name_id "
                      "WHERE pn.name = ? AND pv.version = ?", (name, version)))[0]


def _server(tmp_path, monkeypatch):
    db = connect(tmp_path / "duck.db")
    migrate(db)
    _register(db, "sales", "1.0.0", "outlet", "ponds/sales/1.0.0", _cfg(["revenue"]), _RIPPLES)
    pv = _pv(db, "sales", "1.0.0")
    for t in ("revenue", "secret_internal"):
        db.execute('INSERT OR IGNORE INTO pond_version_schema (pond_version_id, "table", "column", type) '
                   "VALUES (?, ?, 'id', 'INTEGER')", (pv, t))
    db.commit()
    for table, n in (("revenue", 7), ("secret_internal", 3)):
        data_dir = pond_data_dir(tmp_path, "sales", 1, None)
        data_dir.mkdir()
        con = duckdb.connect()
        with data_dir.copy_to(f"{table}.parquet") as uri:
            con.execute(f"COPY (SELECT range AS id FROM range({n})) TO '{uri}' (FORMAT PARQUET)")
        con.close()
    driver = Driver(db, tmp_path, "http://x", NoopLauncher())
    server = PgWireServer(driver, host="127.0.0.1", port=0)
    port = server.start()
    return server, port


def test_pg_client_queries_the_serving_surface(tmp_path, monkeypatch):
    server, port = _server(tmp_path, monkeypatch)
    try:
        conn = pg8000.Connection("duck", host="127.0.0.1", port=port, database="clouddemo", password="")
        # A real pg client (extended protocol) runs a query and parses the rows.
        rows = conn.run("SELECT count(*) AS n FROM sales.revenue")
        assert rows == [[7]]
        # An explicit _v1 pin works.
        assert conn.run("SELECT count(*) FROM sales_v1.revenue") == [[7]]
        # DuckDB answers information_schema introspection over the served schemas.
        schemas = {r[0] for r in conn.run(
            "SELECT DISTINCT table_schema FROM information_schema.tables")}
        assert "sales" in schemas and "sales_v1" in schemas
        # A bad query surfaces a pg error, not a hang.
        from pg8000.native import DatabaseError
        with pytest.raises(DatabaseError):
            conn.run("SELECT * FROM does_not_exist")
        conn.close()
    finally:
        server.stop()


# ─── Postgres types (pg_types) and the protocol details drivers rely on ──────────────────────────────────────────

@pytest.mark.parametrize("value", ["0", "1", "-1", "1234.56", "-0.0001", "0.10", "10000", "99999999.99990000",
                                   "12345678901234567890.123", "0.00", "-987654321098765432109876543210"])
def test_a_numeric_round_trips_through_postgres_binary(value):
    from decimal import Decimal

    from duckstring.catchment.pg_types import NUMERIC, _numeric_from, encode

    d = Decimal(value)
    back = _numeric_from(encode(NUMERIC, d, True))
    assert back == d and back.as_tuple().exponent == d.as_tuple().exponent


def test_numeric_binary_matches_postgres_bytes():
    from decimal import Decimal

    from duckstring.catchment.pg_types import NUMERIC, encode

    # Postgres sends 1234.56 as ndigits 2, weight 0, positive, dscale 2, digits [1234, 5600].
    assert encode(NUMERIC, Decimal("1234.56"), True) == bytes.fromhex("0002 0000 0000 0002 04d2 15e0")
    # and 0.0001 as one digit [1] at weight -1.
    assert encode(NUMERIC, Decimal("0.0001"), True) == bytes.fromhex("0001 ffff 0000 0004 0001")


def test_timestamps_and_dates_count_from_the_postgres_epoch():
    from datetime import date, datetime, timezone

    from duckstring.catchment.pg_types import DATE, TIMESTAMP, TIMESTAMPTZ, decode_param, encode

    assert encode(DATE, date(2000, 1, 2), True) == (1).to_bytes(4, "big")
    assert encode(TIMESTAMP, datetime(2000, 1, 1, 0, 0, 1), True) == (1_000_000).to_bytes(8, "big")
    aware = datetime(2026, 11, 5, 13, 0, tzinfo=timezone.utc)
    assert decode_param(1184, True, encode(TIMESTAMPTZ, aware, True)) == aware
    assert encode(TIMESTAMPTZ, aware, False) == b"2026-11-05 13:00:00+00"


def test_duckdb_types_map_to_postgres_oids():
    from duckstring.catchment.pg_types import pg_type, typmod

    assert [pg_type(t).oid for t in ("INTEGER", "BIGINT", "HUGEINT", "DECIMAL(12,2)", "VARCHAR", "INTEGER[]")] == \
        [23, 20, 1700, 1700, 1043, 25]
    assert typmod("DECIMAL(12,2)") == (12 << 16 | 2) + 4 and typmod("VARCHAR") == -1


def test_type_loading_is_answered_in_the_columns_each_driver_selects():
    from duckstring.catchment.pg_types import catalog_answer

    v4 = catalog_answer("/*** Load all supported types ***/\nSELECT ns.nspname, a.typname, a.oid, a.typrelid, "
                        "a.typbasetype,\nCASE WHEN pg_proc.proname='array_recv' THEN 'a' ELSE a.typtype END AS type,\n"
                        "CASE WHEN a.typtype='r' THEN rngsubtype ELSE 0 END AS elemoid,\n"
                        "CASE WHEN pg_proc.proname IN ('array_recv','oidvectorrecv') THEN 3 ELSE 0 END AS ord\n"
                        "FROM pg_type AS a JOIN pg_proc ON pg_proc.oid = a.typreceive LEFT JOIN pg_range ON 1=1")
    assert v4[0] == ["nspname", "typname", "oid", "typrelid", "typbasetype", "type", "elemoid", "ord"]
    assert ("pg_catalog", "int4", "23", "0", "0", "b", "0", "0") in v4[1]
    v10 = catalog_answer("SELECT ns.nspname, t.oid, t.typname, t.typtype, t.typnotnull, t.elemtypoid FROM (\n"
                         "  -- Arrays have typtype=b\n  SELECT 1 FROM pg_type AS typ\n"
                         "  LEFT JOIN pg_proc AS proc ON proc.oid = typ.typreceive\n"
                         "  LEFT JOIN pg_range ON (pg_range.rngtypid = typ.oid)) t")
    assert v10[0] == ["nspname", "oid", "typname", "typtype", "typnotnull", "elemtypoid"]
    assert ("pg_catalog", "1007", "_int4", "a", "f", "23") in v10[1]
    assert ("pg_catalog", "23", "int4", "b", "f", None) in v10[1]
    assert catalog_answer("SELECT * FROM sales.revenue") is None


def test_session_commands_complete_without_reaching_duckdb(tmp_path, monkeypatch):
    server, port = _server(tmp_path, monkeypatch)
    try:
        conn = pg8000.Connection("duck", host="127.0.0.1", port=port, database="d", password="")
        # Npgsql's reset of a pooled connection that has prepared statements, in one message.
        conn.run("SET SESSION AUTHORIZATION DEFAULT;RESET ALL;UNLISTEN *;SELECT pg_advisory_unlock_all();"
                 "DISCARD SEQUENCES;DISCARD TEMP")
        conn.run("DISCARD ALL")
        conn.run("SET extra_float_digits = 3")
        conn.run("BEGIN")
        assert conn.run("SELECT count(*) FROM sales.revenue") == [[7]]
        conn.run("COMMIT")
        conn.close()
    finally:
        server.stop()


def test_a_server_with_a_certificate_refuses_unencrypted_clients(tmp_path, monkeypatch):
    from test_pg_wire_npgsql import _self_signed

    from duckstring.catchment.pg_wire import tls_context

    server, port = _server(tmp_path, monkeypatch)
    server.tls = tls_context(*map(str, _self_signed(tmp_path)))
    try:
        from pg8000.native import InterfaceError

        with pytest.raises((InterfaceError, pg8000.DatabaseError)) as refused:
            pg8000.Connection("duck", host="127.0.0.1", port=port, database="d", password="x", ssl_context=False)
        assert "TLS" in str(refused.value)
        import ssl

        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        conn = pg8000.Connection("duck", host="127.0.0.1", port=port, database="d", password="x", ssl_context=ctx)
        assert conn.run("SELECT count(*) FROM sales.revenue") == [[7]]
        conn.close()
    finally:
        server.stop()


def test_a_key_without_a_certificate_is_refused():
    from duckstring.catchment.pg_wire import tls_context

    assert tls_context(None, None) is None
    with pytest.raises(ValueError):
        tls_context(None, "server.key")
