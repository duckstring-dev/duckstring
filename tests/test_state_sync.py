"""Tier-1/2 state sync (plans/storage-decoupling.md Part 2): ``duck.db`` (+ ledgers/registries) snapshot
to a backup URI and restore on a fresh node — what makes scale-to-zero / ephemeral disk safe."""

from __future__ import annotations

import sqlite3

from duckstring.catchment.state_sync import (
    checkpoint_full,
    checkpoint_tier1,
    parse_interval_seconds,
    restore_state,
    restore_state_if_empty,
)


def _seed_state(root):
    """A minimal state root: duck.db + a per-Pond ledger and registry under ponds/."""
    root.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(root / "duck.db"))
    con.execute("CREATE TABLE t (id INTEGER)")
    con.execute("INSERT INTO t VALUES (42)")
    con.commit()
    con.close()
    line = root / "ponds" / "p" / "m1"
    line.mkdir(parents=True, exist_ok=True)
    (line / "registry.duckdb").write_bytes(b"REGISTRY-BYTES")
    led = sqlite3.connect(str(line / "pond.db"))
    led.execute("CREATE TABLE ledger (k TEXT)")
    led.commit()
    led.close()
    (root / "secrets.json").write_text("{}")  # must NOT travel in a bundle


def test_parse_interval_seconds():
    assert parse_interval_seconds("30s") == 30
    assert parse_interval_seconds("5m") == 300
    assert parse_interval_seconds("1h") == 3600
    assert parse_interval_seconds("90") == 90  # bare number = seconds
    assert parse_interval_seconds(None) == 60.0  # default
    assert parse_interval_seconds("garbage") == 60.0


def test_no_backup_is_noop(tmp_path):
    _seed_state(tmp_path / "root")
    assert checkpoint_tier1(tmp_path / "root", None) is False
    assert checkpoint_full(tmp_path / "root", None) is False
    assert restore_state_if_empty(tmp_path / "fresh", None) is False


def test_tier1_checkpoint_and_restore(tmp_path):
    root = tmp_path / "root"
    backup = str(tmp_path / "backup")  # a local "object store" stand-in
    _seed_state(root)

    assert checkpoint_tier1(root, backup) is True
    assert (tmp_path / "backup" / "duck.db").exists()

    fresh = tmp_path / "fresh"
    assert restore_state_if_empty(fresh, backup) is True
    con = sqlite3.connect(str(fresh / "duck.db"))
    assert con.execute("SELECT id FROM t").fetchone() == (42,)
    con.close()


def test_full_checkpoint_bundles_ledgers_and_registries(tmp_path):
    root = tmp_path / "root"
    backup = str(tmp_path / "backup")
    _seed_state(root)

    assert checkpoint_full(root, backup) is True
    assert (tmp_path / "backup" / "state.tar").exists()
    assert (tmp_path / "backup" / "duck.db").exists()  # Tier-1 refreshed too

    fresh = tmp_path / "fresh"
    assert restore_state(fresh, backup) is True
    # the warm bundle brings back the registries + ledgers
    assert (fresh / "ponds" / "p" / "m1" / "registry.duckdb").read_bytes() == b"REGISTRY-BYTES"
    assert (fresh / "ponds" / "p" / "m1" / "pond.db").exists()
    assert sqlite3.connect(str(fresh / "duck.db")).execute("SELECT id FROM t").fetchone() == (42,)
    # the write-only secret store never travels in a bundle
    assert not (fresh / "secrets.json").exists()


def test_restore_if_empty_skips_when_state_present(tmp_path):
    root = tmp_path / "root"
    backup = str(tmp_path / "backup")
    _seed_state(root)
    checkpoint_tier1(root, backup)
    # mutate the live duck.db so we can detect an unwanted clobber
    con = sqlite3.connect(str(root / "duck.db"))
    con.execute("UPDATE t SET id = 99")
    con.commit()
    con.close()
    assert restore_state_if_empty(root, backup) is False  # never clobbers a live root
    assert sqlite3.connect(str(root / "duck.db")).execute("SELECT id FROM t").fetchone() == (99,)
