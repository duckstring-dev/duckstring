"""The data-root writer lease (plans/storage-decoupling.md): refuse a *second* Catchment on one lake so
two writers can't race the Iceberg catalog pointer. A lease, not a lock — same-id reclaim, TTL takeover,
read-back race detection."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from duckstring.catchment.data_lease import (
    LEASE_NAME,
    LeaseConflict,
    acquire_lease,
    release_lease,
    renew_lease,
)
from duckstring.storage import ObjectStorage, get_storage


def _write_lease(storage, owner, renewed_at):
    storage.write_text(json.dumps({
        "catchment_id": owner, "host": "h", "pid": 1,
        "acquired_at": renewed_at.isoformat(), "renewed_at": renewed_at.isoformat(), "ttl_seconds": 120,
    }), LEASE_NAME)


def test_acquire_on_empty(tmp_path):
    s = get_storage(tmp_path)
    rec = acquire_lease(s, "cat-A")
    assert rec["catchment_id"] == "cat-A"
    assert s.exists(LEASE_NAME)


def test_same_owner_reclaims_and_preserves_acquired_at(tmp_path):
    s = get_storage(tmp_path)
    first = acquire_lease(s, "cat-A")
    again = acquire_lease(s, "cat-A")  # a restart of the same Catchment
    assert again["acquired_at"] == first["acquired_at"]  # original start time preserved


def test_different_live_owner_is_refused(tmp_path):
    s = get_storage(tmp_path)
    _write_lease(s, "cat-A", datetime.now(timezone.utc))  # fresh
    with pytest.raises(LeaseConflict):
        acquire_lease(s, "cat-B")


def test_expired_owner_is_taken_over(tmp_path):
    s = get_storage(tmp_path)
    _write_lease(s, "cat-A", datetime.now(timezone.utc) - timedelta(seconds=300))  # stale > ttl
    rec = acquire_lease(s, "cat-B")
    assert rec["catchment_id"] == "cat-B"


def test_force_takeover_steals_a_live_lease(tmp_path):
    s = get_storage(tmp_path)
    _write_lease(s, "cat-A", datetime.now(timezone.utc))
    rec = acquire_lease(s, "cat-B", force=True)
    assert rec["catchment_id"] == "cat-B"


def test_force_takeover_via_env(tmp_path, monkeypatch):
    s = get_storage(tmp_path)
    _write_lease(s, "cat-A", datetime.now(timezone.utc))
    monkeypatch.setenv("DUCKSTRING_FORCE_TAKEOVER", "1")
    assert acquire_lease(s, "cat-B")["catchment_id"] == "cat-B"


def test_renew_refreshes_and_detects_theft(tmp_path):
    s = get_storage(tmp_path)
    acquire_lease(s, "cat-A")
    assert renew_lease(s, "cat-A") is True
    _write_lease(s, "cat-B", datetime.now(timezone.utc))  # someone stole it
    assert renew_lease(s, "cat-A") is False  # detected → caller treats as fatal divergence


def test_release_only_if_ours(tmp_path):
    s = get_storage(tmp_path)
    acquire_lease(s, "cat-A")
    release_lease(s, "cat-B")  # not ours → no-op
    assert s.exists(LEASE_NAME)
    release_lease(s, "cat-A")
    assert not s.exists(LEASE_NAME)


def test_lease_on_object_store_memory():
    o = ObjectStorage("memory://lake/duckstring")
    acquire_lease(o, "cat-A")
    assert o.read_text(LEASE_NAME) is not None
    with pytest.raises(LeaseConflict):
        acquire_lease(o, "cat-B")


def test_create_app_refuses_second_catchment_on_shared_data_root(tmp_path):
    from duckstring.catchment.app import create_app

    data_root = str(tmp_path / "lake")
    create_app(tmp_path / "state1", data_root=data_root)  # first owner acquires the lease at boot
    with pytest.raises(LeaseConflict):
        create_app(tmp_path / "state2", data_root=data_root)  # a different Catchment id → refused
