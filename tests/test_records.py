"""Records (plans/records-and-media.md): the managed PostgreSQL and each App line's schema and role.

Runs against a real PostgreSQL on this machine (`duckstring install postgres`) and skips without one; CI's
records job installs it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from duckstring.catchment.db import connect, migrate
from duckstring.catchment.records import READER, Records, RecordsError, check_line_name, line_role
from duckstring.install import find_postgres

PG = find_postgres()
needs_pg = pytest.mark.skipif(PG is None, reason="no PostgreSQL here (duckstring install postgres)")


def _short_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def records(tmp_path):
    db = connect(tmp_path / "duck.db")
    migrate(db)
    reserved = []
    r = Records(tmp_path, db, reserve=reserved.append, release=lambda: reserved.append(-1))
    r.reserved = reserved
    yield r
    r.stop()


def _as(records: Records, role: str, sql: str) -> subprocess.CompletedProcess:
    endpoint = records.ensure()
    env = {**os.environ, "PGPASSWORD": records.password(role)}
    return subprocess.run([str(PG[0] / "psql"), "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                           "-d", endpoint.conninfo(user=role), "-c", sql], capture_output=True, text=True, env=env)


def test_line_names_are_plain_identifiers():
    assert line_role("feedback", 2) == "feedback_v2"
    for bad in ("Feedback", "feed-back", "1st", "x" * 49, 'a"b'):
        with pytest.raises(RecordsError):
            check_line_name(bad)


@needs_pg
@pytest.mark.timeout(60)
def test_a_line_writes_its_schema_and_reads_the_others(records):
    records.provision("feedback", 1)
    records.provision("admin", 1)
    assert records.reserved == [records.memory]  # set aside once, while it runs

    made = _as(records, "feedback_v1", "CREATE TABLE ratings (id int PRIMARY KEY, score int);"
                                       "INSERT INTO ratings VALUES (1, 5), (2, 3);")
    assert made.returncode == 0, made.stderr
    # Another App reads it, and can't write it.
    read = _as(records, "admin_v1", "SELECT sum(score) FROM feedback_v1.ratings")
    assert read.stdout.strip() == "8", read.stderr
    write = _as(records, "admin_v1", "INSERT INTO feedback_v1.ratings VALUES (3, 1)")
    assert write.returncode != 0 and "permission denied" in write.stderr
    # Its own unqualified tables land in its own schema, and nowhere else is writable.
    _as(records, "admin_v1", "CREATE TABLE notes (t text)")
    assert _as(records, "admin_v1", "SELECT count(*) FROM admin_v1.notes").stdout.strip() == "0"
    assert _as(records, "admin_v1", "CREATE TABLE public.x (i int)").returncode != 0
    # Ponds read every line's tables.
    assert _as(records, "duckstring_ponds", "SELECT count(*) FROM feedback_v1.ratings").stdout.strip() == "2"
    assert READER in _as(records, "feedback_v1", "SELECT string_agg(b.rolname, ',') FROM pg_auth_members m "
                                                 "JOIN pg_roles b ON m.roleid = b.oid JOIN pg_roles r ON "
                                                 "m.member = r.oid WHERE r.rolname = 'feedback_v1'").stdout


@needs_pg
@pytest.mark.timeout(60)
def test_a_frozen_line_can_be_read_but_not_signed_in_to(records):
    records.provision("feedback", 1)
    _as(records, "feedback_v1", "CREATE TABLE ratings (id int)")
    records.freeze("feedback", 1)
    assert "not permitted to log in" in _as(records, "feedback_v1", "SELECT 1").stderr
    assert _as(records, "duckstring_ponds", "SELECT count(*) FROM feedback_v1.ratings").stdout.strip() == "0"
    records.provision("feedback", 1)  # provisioning again lets it sign in again
    assert _as(records, "feedback_v1", "SELECT 1").returncode == 0


@needs_pg
@pytest.mark.timeout(60)
def test_dropping_a_line_removes_its_schema_and_role(records):
    records.provision("feedback", 1)
    _as(records, "feedback_v1", "CREATE TABLE ratings (id int)")
    records.drop("feedback", 1)
    assert records.psql("SELECT count(*) FROM pg_namespace WHERE nspname = 'feedback_v1'").strip() == "0"
    assert records.psql("SELECT count(*) FROM pg_roles WHERE rolname = 'feedback_v1'").strip() == "0"


@needs_pg
@pytest.mark.timeout(60)
def test_a_restarted_catchment_adopts_or_restarts_its_postgres(tmp_path, records):
    records.provision("feedback", 1)
    _as(records, "feedback_v1", "CREATE TABLE ratings (id int); INSERT INTO ratings VALUES (7)")
    # A second Records over the same root (a Catchment process that replaced the first without stopping it)
    # adopts the running server rather than failing on its lock.
    again = Records(tmp_path, records.db)
    assert _as(again, "feedback_v1", "SELECT id FROM ratings").stdout.strip() == "7"
    records.stop()
    again.pg._adopted = None  # its record of the adopted server is gone with it
    again._ready = None
    assert _as(again, "feedback_v1", "SELECT id FROM ratings").stdout.strip() == "7"  # started afresh, data kept
    again.stop()


@needs_pg
@pytest.mark.timeout(60)
def test_the_target_is_fixed_once_a_line_has_records(records):
    records.configure(target="postgres://someone:pw@db.example.com/records")
    assert records.target.startswith("postgres://")
    records.configure(target="managed")
    with records.db:
        records.db.execute("INSERT INTO app_data (name, major, records, created_at) VALUES ('f', 1, 1, 'now')")
    with pytest.raises(RecordsError, match="move"):
        records.configure(target="postgres://someone:pw@db.example.com/records")
    with pytest.raises(RecordsError):
        records.configure(target="mysql://x/y")


def test_an_external_uri_resolves_its_secrets(tmp_path, monkeypatch):
    from duckstring.catchment.records import parse_external

    monkeypatch.setenv("RECORDS_PW", "s3cret")
    endpoint = parse_external("postgres://admin:${env:RECORDS_PW}@db.example.com:6543/apps?sslmode=require")
    assert (endpoint.host, endpoint.port, endpoint.database, endpoint.admin_password, endpoint.sslmode) == (
        "db.example.com", 6543, "apps", "s3cret", "require")
