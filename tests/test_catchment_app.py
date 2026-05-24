from __future__ import annotations

import io
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

from duckstring.catchment.db import connect, migrate

_DEMO_DIR = Path(__file__).parent.parent / "src" / "duckstring" / "demo"


# ---------------------------------------------------------------------------
# DB unit tests — no HTTP layer
# ---------------------------------------------------------------------------

def test_migrate_creates_tables(tmp_path):
    con = connect(tmp_path / "duck.db")
    migrate(con)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"pond", "pond_version", "ripple", "ripple_to_ripple", "pond_to_pond",
            "pond_trigger", "demand", "watermark", "pond_run", "ripple_run"} <= tables


def test_migrate_is_idempotent(tmp_path):
    con = connect(tmp_path / "duck.db")
    migrate(con)
    migrate(con)  # second call must not raise or duplicate data
    versions = [r[0] for r in con.execute("SELECT version FROM schema_migrations ORDER BY version")]
    assert versions == sorted(set(versions))


def test_foreign_keys_enforced(tmp_path):
    con = connect(tmp_path / "duck.db")
    migrate(con)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO pond_version (pond_id, version, major, source_path) VALUES (999, '1.0.0', 1, 'x')")
        con.commit()


# ---------------------------------------------------------------------------
# HTTP layer tests — use catchment_client fixture from conftest
# ---------------------------------------------------------------------------

def test_health(catchment_client):
    r = catchment_client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_db_available_on_app_state(catchment_client):
    assert catchment_client.app.state.db is not None


def test_root_dir_on_app_state(catchment_client, tmp_path):
    # tmp_path is the same fixture instance shared within the test,
    # but catchment_client uses its own tmp_path injection — just check type.
    assert catchment_client.app.state.root.is_dir()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_pond_zip(*, toml_text: str, pond_py_text: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pond.toml", toml_text)
        if pond_py_text:
            zf.writestr("src/pond.py", pond_py_text)
    return buf.getvalue()


def _deploy(client, *, name: str, version: str, kind: str, toml_text: str, pond_py_text: str = ""):
    archive = make_pond_zip(toml_text=toml_text, pond_py_text=pond_py_text)
    return client.post(
        "/api/deploy",
        files={"pond": ("pond.zip", archive, "application/zip")},
        data={"name": name, "version": version, "type": kind},
    )


def _db(catchment_client):
    return catchment_client.app.state.db


def _seed(catchment_client, pond: str, ripple: str):
    """Seed a DuckDB table with two rows for testing data endpoints."""
    import duckdb
    root = catchment_client.app.state.root
    reg_path = root / "ponds" / pond / "registry.duckdb"
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg = duckdb.connect(str(reg_path))
    reg.execute(f'CREATE SCHEMA IF NOT EXISTS "{pond}"')
    reg.execute(f'CREATE OR REPLACE TABLE "{pond}"."{ripple}" AS SELECT 1 AS id, \'a\' AS val UNION ALL SELECT 2, \'b\'')
    reg.close()


# ---------------------------------------------------------------------------
# Deploy tests
# ---------------------------------------------------------------------------

INLET_TOML = """\
[pond]
name = "inlet"
version = "1.0.0"
type = "inlet"
"""

POND_TOML = """\
[pond]
name = "mypond"
version = "1.0.0"

[sources]
inlet = "1.0.0"
"""

OUTLET_TOML = """\
[pond]
name = "outlet"
version = "2.0.0"
type = "outlet"

[sources]
mypond = "1.0.0"
upstream = "2.0.0?"
"""

POND_PY_TWO_RIPPLES = """\
from duckstring import ripple

@ripple
def load(pond): ...

@ripple(parents=[load])
def clean(pond): ...
"""


def test_deploy_local_registers_pond(catchment_client):
    r = _deploy(catchment_client, name="inlet", version="1.0.0", kind="inlet", toml_text=INLET_TOML)
    assert r.status_code == 200
    row = _db(catchment_client).execute("SELECT name, kind FROM pond WHERE name = 'inlet'").fetchone()
    assert row == ("inlet", "inlet")


def test_deploy_local_registers_version(catchment_client):
    r = _deploy(catchment_client, name="inlet", version="1.0.0", kind="inlet", toml_text=INLET_TOML)
    assert r.status_code == 200
    row = _db(catchment_client).execute(
        "SELECT version, major, is_active, source_path FROM pond_version WHERE version = '1.0.0'"
    ).fetchone()
    assert row == ("1.0.0", 1, 1, "ponds/inlet/1.0.0")


def test_deploy_local_registers_sources(catchment_client):
    # Deploy inlet first so it exists as a pond
    _deploy(catchment_client, name="inlet", version="1.0.0", kind="inlet", toml_text=INLET_TOML)
    r = _deploy(catchment_client, name="mypond", version="1.0.0", kind="pond", toml_text=POND_TOML)
    assert r.status_code == 200

    db = _db(catchment_client)
    (pond_id,) = db.execute("SELECT id FROM pond WHERE name = 'mypond'").fetchone()
    (version_id,) = db.execute("SELECT id FROM pond_version WHERE pond_id = ?", (pond_id,)).fetchone()
    edges = db.execute(
        "SELECT p.name, e.source_major, e.min_version, e.required "
        "FROM pond_to_pond e JOIN pond p ON p.id = e.source_pond_id WHERE e.pond_version_id = ?",
        (version_id,),
    ).fetchall()
    assert len(edges) == 1
    assert edges[0] == ("inlet", 1, "1.0.0", 1)


def test_deploy_local_required_and_optional_sources(catchment_client):
    r = _deploy(catchment_client, name="outlet", version="2.0.0", kind="outlet", toml_text=OUTLET_TOML)
    assert r.status_code == 200

    db = _db(catchment_client)
    (pond_id,) = db.execute("SELECT id FROM pond WHERE name = 'outlet'").fetchone()
    (version_id,) = db.execute("SELECT id FROM pond_version WHERE pond_id = ?", (pond_id,)).fetchone()
    edges = {
        row[0]: row
        for row in db.execute(
            "SELECT p.name, e.required FROM pond_to_pond e JOIN pond p ON p.id = e.source_pond_id WHERE e.pond_version_id = ?",
            (version_id,),
        ).fetchall()
    }
    assert edges["mypond"][1] == 1    # required
    assert edges["upstream"][1] == 0  # optional (?)


def test_deploy_local_registers_ripples(catchment_client):
    r = _deploy(
        catchment_client,
        name="inlet", version="1.0.0", kind="inlet",
        toml_text=INLET_TOML, pond_py_text=POND_PY_TWO_RIPPLES,
    )
    assert r.status_code == 200

    db = _db(catchment_client)
    (version_id,) = db.execute(
        "SELECT pv.id FROM pond_version pv JOIN pond p ON p.id = pv.pond_id WHERE p.name = 'inlet'"
    ).fetchone()
    ripples = {r[0] for r in db.execute("SELECT name FROM ripple WHERE pond_version_id = ?", (version_id,)).fetchall()}
    assert ripples == {"load", "clean"}


def test_deploy_local_registers_ripple_edges(catchment_client):
    r = _deploy(
        catchment_client,
        name="inlet", version="1.0.0", kind="inlet",
        toml_text=INLET_TOML, pond_py_text=POND_PY_TWO_RIPPLES,
    )
    assert r.status_code == 200

    db = _db(catchment_client)
    (version_id,) = db.execute(
        "SELECT pv.id FROM pond_version pv JOIN pond p ON p.id = pv.pond_id WHERE p.name = 'inlet'"
    ).fetchone()
    edges = db.execute(
        """SELECT sink.name, src.name FROM ripple_to_ripple e
           JOIN ripple sink ON sink.id = e.sink_id
           JOIN ripple src  ON src.id  = e.source_id
           WHERE sink.pond_version_id = ?""",
        (version_id,),
    ).fetchall()
    assert edges == [("clean", "load")]


def test_deploy_activates_new_version(catchment_client):
    _deploy(catchment_client, name="inlet", version="1.0.0", kind="inlet", toml_text=INLET_TOML)
    new_toml = INLET_TOML.replace("1.0.0", "1.1.0")
    _deploy(catchment_client, name="inlet", version="1.1.0", kind="inlet", toml_text=new_toml)

    db = _db(catchment_client)
    rows = db.execute(
        "SELECT version, is_active FROM pond_version ORDER BY version"
    ).fetchall()
    assert dict(rows) == {"1.0.0": 0, "1.1.0": 1}


def test_deploy_two_majors_both_active(catchment_client):
    _deploy(catchment_client, name="inlet", version="1.0.0", kind="inlet", toml_text=INLET_TOML)
    v2_toml = INLET_TOML.replace("1.0.0", "2.0.0")
    _deploy(catchment_client, name="inlet", version="2.0.0", kind="inlet", toml_text=v2_toml)

    db = _db(catchment_client)
    rows = db.execute(
        "SELECT version, is_active FROM pond_version ORDER BY version"
    ).fetchall()
    assert dict(rows) == {"1.0.0": 1, "2.0.0": 1}


def test_deploy_bad_zip_returns_422(catchment_client):
    r = catchment_client.post(
        "/api/deploy",
        files={"pond": ("pond.zip", b"not a zip", "application/zip")},
        data={"name": "x", "version": "1.0.0", "type": "pond"},
    )
    assert r.status_code == 422


def test_deploy_cycle_returns_422(catchment_client):
    # a → b → a is a cycle; deploy of b (which declares a as source) should be rejected
    # if a already declares b as its source.
    _deploy(catchment_client, name="a", version="1.0.0", kind="pond",
            toml_text='[pond]\nname="a"\nversion="1.0.0"\n\n[sources]\nb="1.0.0"\n')
    r = _deploy(catchment_client, name="b", version="1.0.0", kind="pond",
                toml_text='[pond]\nname="b"\nversion="1.0.0"\n\n[sources]\na="1.0.0"\n')
    assert r.status_code == 422
    assert "Cycle" in r.json()["detail"]

    # b's version must not have been registered (transaction rolled back)
    db = _db(catchment_client)
    assert db.execute(
        "SELECT COUNT(*) FROM pond_version pv JOIN pond p ON p.id = pv.pond_id WHERE p.name = 'b'"
    ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Data — query tests
# ---------------------------------------------------------------------------

def test_query_returns_rows(catchment_client):
    _seed(catchment_client, "outlet", "daily")
    r = catchment_client.post("/api/query", json={"pond": "outlet", "ripple": "daily"})
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    assert len(rows) == 2
    assert rows[0]["id"] == 1


def test_query_custom_sql(catchment_client):
    _seed(catchment_client, "outlet", "daily")
    r = catchment_client.post("/api/query", json={"pond": "outlet", "sql": 'SELECT * FROM "outlet"."daily" WHERE id = 1'})
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_query_csv_format(catchment_client):
    _seed(catchment_client, "outlet", "daily")
    r = catchment_client.post("/api/query", json={"pond": "outlet", "ripple": "daily", "format": "csv"})
    assert r.status_code == 200
    assert b"id" in r.content
    assert b"val" in r.content


def test_query_parquet_format(catchment_client):
    _seed(catchment_client, "outlet", "daily")
    r = catchment_client.post("/api/query", json={"pond": "outlet", "ripple": "daily", "format": "parquet"})
    assert r.status_code == 200
    # Parquet magic bytes
    assert r.content[:4] == b"PAR1"


def test_query_missing_relation_returns_400(catchment_client):
    r = catchment_client.post("/api/query", json={"pond": "ghost", "ripple": "nothing"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Data — get ripple tests
# ---------------------------------------------------------------------------

def test_data_get_returns_zip(catchment_client):
    _seed(catchment_client, "outlet", "daily")
    r = catchment_client.get("/api/ponds/outlet/ripples/daily")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert "daily.parquet" in zf.namelist()


def test_data_get_missing_returns_404(catchment_client):
    r = catchment_client.get("/api/ponds/ghost/ripples/nothing")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Demo pond deployment
# ---------------------------------------------------------------------------

def _read_toml(path: Path) -> dict:
    if sys.version_info >= (3, 11):
        import tomllib
        return tomllib.loads(path.read_text(encoding="utf-8"))
    import tomli
    return tomli.loads(path.read_text(encoding="utf-8"))


def _zip_dir(path: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(path.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(path))
    return buf.getvalue()


def _deploy_demo(client, name: str):
    pond_dir = _DEMO_DIR / name
    info = _read_toml(pond_dir / "pond.toml")["pond"]
    return client.post(
        "/api/deploy",
        files={"pond": ("pond.zip", _zip_dir(pond_dir), "application/zip")},
        data={"name": info["name"], "version": info["version"], "type": info.get("type", "pond")},
    )


def test_deploy_demo_ponds(catchment_client):
    for name in ("inlet", "pond", "outlet"):
        r = _deploy_demo(catchment_client, name)
        assert r.status_code == 200, f"Deploy of {name} failed: {r.text}"

    db = _db(catchment_client)

    # All three ponds registered and active
    active = {
        row[0]
        for row in db.execute(
            "SELECT p.name FROM pond_version pv JOIN pond p ON p.id = pv.pond_id WHERE pv.is_active = 1"
        ).fetchall()
    }
    assert active == {"inlet", "pond", "outlet"}

    # Inter-pond source edges
    edges = {
        (row[0], row[1])
        for row in db.execute(
            """SELECT sink.name, src.name
               FROM pond_to_pond e
               JOIN pond_version pv ON pv.id = e.pond_version_id
               JOIN pond sink ON sink.id = pv.pond_id
               JOIN pond src  ON src.id  = e.source_pond_id"""
        ).fetchall()
    }
    assert ("pond", "inlet") in edges
    assert ("outlet", "pond") in edges

    # Ripples registered for each pond
    for pond_name, expected in [("inlet", {"daily"}), ("pond", {"clean"}), ("outlet", {"daily"})]:
        ripples = {
            row[0]
            for row in db.execute(
                """SELECT r.name FROM ripple r
                   JOIN pond_version pv ON pv.id = r.pond_version_id
                   JOIN pond p ON p.id = pv.pond_id
                   WHERE p.name = ?""",
                (pond_name,),
            ).fetchall()
        }
        assert ripples == expected, f"Wrong ripples for {pond_name}: {ripples}"
