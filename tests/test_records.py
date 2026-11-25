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


@needs_pg
@pytest.mark.timeout(60)
def test_a_line_is_copied_whole_into_another(records):
    records.provision("feedback", 1)
    made = _as(records, "feedback_v1", """
        CREATE TABLE ratings (id serial PRIMARY KEY, score int CHECK (score BETWEEN 1 AND 5));
        CREATE INDEX ratings_score ON ratings (score);
        INSERT INTO ratings (score) VALUES (4), (5);
        CREATE VIEW good AS SELECT * FROM ratings WHERE score >= 4;""")
    assert made.returncode == 0, made.stderr
    records.copy_line(("feedback", 1), ("feedback", 2))
    # The copy is the new line's: its role owns it, writes it (the sequence carries on), and finds it unqualified.
    assert _as(records, "feedback_v2", "INSERT INTO ratings (score) VALUES (3) RETURNING id").stdout.strip() == "3"
    assert _as(records, "feedback_v2", "SELECT count(*) FROM good").stdout.strip() == "2"
    assert _as(records, "feedback_v2", "INSERT INTO ratings (score) VALUES (9)").returncode != 0  # the CHECK came
    owner = records.psql("SELECT tableowner FROM pg_tables WHERE schemaname = 'feedback_v2'").strip()
    assert owner == "feedback_v2"
    # The source is untouched, still its own role's, and other lines read the copy.
    assert _as(records, "feedback_v1", "SELECT count(*) FROM ratings").stdout.strip() == "2"
    assert _as(records, "duckstring_ponds", "SELECT count(*) FROM feedback_v2.ratings").stdout.strip() == "3"
    # Copying again replaces what the destination held.
    records.copy_line(("feedback", 1), ("feedback", 2))
    assert _as(records, "feedback_v2", "SELECT count(*) FROM ratings").stdout.strip() == "2"
    assert records.psql("SELECT count(*) FROM pg_database WHERE datname LIKE 'duckstring_copy_%'").strip() == "0"


# ─── TLS beyond this machine ───────────────────────────────────────────────────


def _lan_address() -> str | None:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # no packet is sent: this only picks the outgoing interface
            addr = s.getsockname()[0]
        except OSError:
            return None
    return None if addr.startswith("127.") else addr


def test_beyond_this_machine_only_tls_is_accepted():
    from duckstring.catchment.records import hba_lines

    local = hba_lines("127.0.0.1")
    assert "0.0.0.0/0" not in local
    wide = hba_lines("0.0.0.0")
    assert "hostssl all all 0.0.0.0/0 scram-sha-256" in wide
    assert not any(line.startswith("host ") and "0.0.0.0/0" in line for line in wide.splitlines())


@needs_pg
@pytest.mark.timeout(60)
def test_a_postgres_listening_widely_serves_tls_and_refuses_plain_connections(records):
    lan = _lan_address()
    if lan is None:
        pytest.skip("no network address other than loopback")
    records.configure(listen="0.0.0.0", host=lan)
    records.provision("feedback", 1)
    status = records.status()
    assert status["tls"]["on"] and status["tls"]["certificate"] == "self-signed"
    assert len(status["tls"]["fingerprint"].split(":")) == 32

    url = records.line_url("feedback", 1)
    remote = records.url("feedback_v1", remote=True)
    assert f"@{lan}:" in remote and remote.endswith("sslmode=require")
    psql = str(PG[0] / "psql")
    ok = subprocess.run([psql, remote, "-Atc", "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"],
                        capture_output=True, text=True)
    assert ok.stdout.strip() == "t", ok.stderr
    plain = subprocess.run([psql, remote.replace("sslmode=require", "sslmode=disable"), "-Atc", "SELECT 1"],
                           capture_output=True, text=True)
    assert plain.returncode != 0 and "no encryption" in plain.stderr  # no pg_hba line lets it in
    # On this machine a plain connection is still fine.
    assert subprocess.run([psql, url, "-Atc", "SELECT 1"], capture_output=True, text=True).returncode == 0
    records.configure(listen="127.0.0.1")
    with pytest.raises(RecordsError, match="--listen"):
        records.url("feedback_v1", remote=True)


@needs_pg
@pytest.mark.timeout(60)
def test_an_operators_certificate_is_served(tmp_path, records):
    from duckstring.catchment.records import make_certificate

    cert, key = make_certificate(tmp_path / "operator", ["records.example.com"])
    records.configure(tls_cert=str(cert), tls_key=str(key))
    records.provision("feedback", 1)
    assert records.status()["tls"]["certificate"] == "operator"
    got = subprocess.run([str(PG[0] / "psql"), records.line_url("feedback", 1) + "?sslmode=require", "-Atc",
                          "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"], capture_output=True, text=True)
    assert got.stdout.strip() == "t", got.stderr
    with pytest.raises(RecordsError):
        records.configure(tls_cert=str(cert))  # the key goes with it
