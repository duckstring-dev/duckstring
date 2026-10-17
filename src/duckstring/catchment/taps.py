"""Taps (plans/taps.md): named access points on a Pond (one major line) for an app to query.

A Tap has its own key, so a query is classified by the credential it arrives with: the Tap's key means the
Tap's app, and every other key (the web UI's, the CLI's, a notebook's) never sends demand. A query through a
Tap may send the Pond a Refresh, by the floating Refresh's clock evaluated per query (``engine.tap_due``): due
once the newest request, or with none pending the freshness, is ``fresh_within`` old. The query itself is
answered at once from what is published; an app reads the freshness it got from ``/api/ponds/{name}/freshness``.
A Tap's key reads the Pond line's exposed tables: what it may read is the serving core's business, and a Tap only
decides when the Pond refreshes.

The Driver mixes this in. Taps live in memory (``self._taps``, plus a key-hash index read without the lock by
auth on every request) and in ``pond_tap``; the per-Tap counters are observability only and are written in
batches from the scheduler tick, so a busy app doesn't serialise on the Catchment's SQLite lock.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..engine import refresh_now, tap_due
from .auth import TapScope, hash_key

TAP_KEY_PREFIX = "tap_"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def mint_tap_key() -> str:
    """A fresh Tap key. The prefix tells a Tap's key apart from a Catchment key at a glance."""
    return TAP_KEY_PREFIX + secrets.token_urlsafe(24)


@dataclass
class Tap:
    pond: str                    # the pond key "name@major"
    name: str
    key_hash: str
    fresh_within: timedelta
    created_at: str
    last_query_at: datetime | None = None
    query_count: int = 0
    refresh_count: int = 0
    dirty: bool = False          # counters changed since the last flush

    @property
    def scope(self) -> TapScope:
        return TapScope(self.pond, self.name)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "fresh_within_ms": int(self.fresh_within.total_seconds() * 1000),
            "created_at": self.created_at,
            "last_query_at": self.last_query_at.isoformat() if self.last_query_at else None,
            "query_count": self.query_count,
            "refresh_count": self.refresh_count,
        }


class TapsMixin:
    """Tap CRUD, the key index, the per-query policy and the counters. Mixed into ``Driver``."""

    # ─── Loading ──────────────────────────────────────────────────────────────

    def _load_taps(self, pondid_to_key: dict[int, str]) -> None:
        """Rebuild the in-memory Taps from ``pond_tap`` (called by ``reload``; caller holds the lock). Counters
        not yet flushed survive a reload."""
        old = getattr(self, "_taps", {})
        taps: dict[tuple[str, str], Tap] = {}
        for (pond_id, name, key_hash, fresh_ms, created, last_q, n_q, n_r) in self.db.execute(
            "SELECT pond_id, name, key_hash, fresh_within_ms, created_at, last_query_at, "
            "query_count, refresh_count FROM pond_tap ORDER BY pond_id, name"
        ):
            key = pondid_to_key.get(pond_id)
            if key is None:
                continue
            tap = Tap(
                pond=key, name=name, key_hash=key_hash, fresh_within=timedelta(milliseconds=fresh_ms),
                created_at=created,
                last_query_at=datetime.fromisoformat(last_q) if last_q else None,
                query_count=n_q or 0, refresh_count=n_r or 0,
            )
            prev = old.get((key, name))
            if prev is not None and prev.dirty and prev.key_hash == key_hash:
                tap.last_query_at, tap.query_count = prev.last_query_at, prev.query_count
                tap.refresh_count, tap.dirty = prev.refresh_count, True
            taps[(key, name)] = tap
        self._taps = taps
        self._tap_by_hash = {t.key_hash: t.scope for t in taps.values()}

    # ─── Key lookup (auth) ────────────────────────────────────────────────────

    def tap_for_hash(self, key_hash: str) -> TapScope | None:
        """The Tap a key hash belongs to. Read without the lock: the index is replaced whole on change."""
        return getattr(self, "_tap_by_hash", {}).get(key_hash)

    def _tap(self, scope: TapScope) -> Tap:
        tap = self._taps.get((scope.pond, scope.name))
        if tap is None:
            raise KeyError(f"No Tap '{scope.name}' on '{scope.pond}'")
        return tap

    # ─── CRUD ─────────────────────────────────────────────────────────────────

    def add_tap(self, pond: str, name: str, *, fresh_within: timedelta = timedelta(0)) -> tuple[dict, str]:
        """Add a Tap to a Pond line and return it with its key (shown once; only its hash is kept). Raises
        ValueError on a bad name, a duplicate, a negative period or a Spout."""
        name = (name or "").strip()
        if not _NAME_RE.match(name):
            raise ValueError("a Tap's name is letters, digits, '.', '_' or '-' (up to 64), starting with a letter "
                             "or digit")
        if fresh_within < timedelta(0):
            raise ValueError("--fresh-within can't be negative")
        with self.lock:
            meta = self.meta[pond]
            if meta.get("is_spout"):
                raise ValueError("a Spout has no tables to query: add the Tap to its source Pond")
            if (pond, name) in self._taps:
                raise ValueError(f"'{pond}' already has a Tap named '{name}': rotate its key or remove it first")
            key = mint_tap_key()
            created = _now().isoformat()
            self.db.execute(
                "INSERT INTO pond_tap (pond_id, name, key_hash, fresh_within_ms, created_at) VALUES (?, ?, ?, ?, ?)",
                (meta["pond_id"], name, hash_key(key), int(fresh_within.total_seconds() * 1000), created),
            )
            self.db.commit()
            self._reload_taps()
            return self._taps[(pond, name)].as_dict(), key

    def list_taps(self, pond: str) -> list[dict]:
        with self.lock:
            return [t.as_dict() for (p, _), t in sorted(self._taps.items()) if p == pond]

    def remove_tap(self, pond: str, name: str) -> bool:
        """Remove a Tap: its key stops working at once, and demand it already sent stays."""
        with self.lock:
            if (pond, name) not in self._taps:
                return False
            self.db.execute("DELETE FROM pond_tap WHERE pond_id = ? AND name = ?", (self.meta[pond]["pond_id"], name))
            self.db.commit()
            self._reload_taps()
            self._drop_tap_connection(TapScope(pond, name))
            return True

    def rotate_tap(self, pond: str, name: str) -> str:
        """Mint a new key for a Tap; the old one stops working at once."""
        with self.lock:
            self._tap(TapScope(pond, name))
            self._flush_tap_counters()
            key = mint_tap_key()
            self.db.execute("UPDATE pond_tap SET key_hash = ? WHERE pond_id = ? AND name = ?",
                            (hash_key(key), self.meta[pond]["pond_id"], name))
            self.db.commit()
            self._reload_taps()
            return key

    def _reload_taps(self) -> None:
        self._load_taps({m["pond_id"]: k for k, m in self.meta.items()})
        self.state_version += 1

    def _drop_tap_connection(self, scope: TapScope) -> None:
        cache = getattr(self, "_serving_cache", None)
        if cache is not None:
            cache.drop(("tap", scope.pond, scope.name))

    # ─── Scope ────────────────────────────────────────────────────────────────

    def tap_tables(self, scope: TapScope) -> set[str]:
        """The tables a Tap's key may read: its Pond line's exposed tables, as for any read-level key."""
        with self.lock:
            self._tap(scope)
            return self.serviceable(scope.pond)

    # ─── The policy, per query ────────────────────────────────────────────────

    def tap_query(self, scope: TapScope) -> bool:
        """A query arrived through a Tap: count it, and send the Pond a Refresh if its clock says so (returned).
        The query is answered at once either way. Raises KeyError when the Tap has gone (removed or rotated
        mid-request)."""
        with self.lock:
            tap = self._tap(scope)
            now = _now()
            tap.query_count += 1
            tap.last_query_at = now
            tap.dirty = True
            if scope.pond not in self.state.pond_states:
                return False
            sent = tap_due(self.state, scope.pond, now, tap.fresh_within)
            if sent:
                tap.refresh_count += 1
                self.state = refresh_now(self.state, scope.pond, now)
                self._process(now)
            return sent

    # ─── Counters ─────────────────────────────────────────────────────────────

    def _flush_tap_counters(self) -> None:
        """Write the counters of Taps queried since the last flush (caller holds the lock)."""
        dirty = [t for t in getattr(self, "_taps", {}).values() if t.dirty]
        if not dirty:
            return
        for t in dirty:
            self.db.execute(
                "UPDATE pond_tap SET last_query_at = ?, query_count = ?, refresh_count = ? "
                "WHERE key_hash = ?",
                (t.last_query_at.isoformat() if t.last_query_at else None, t.query_count, t.refresh_count,
                 t.key_hash),
            )
            t.dirty = False
        self.db.commit()

    def taps_status(self, pond: str) -> list[dict]:
        """A Pond's Taps for ``/api/status`` (never keys). Caller holds the lock."""
        return [t.as_dict() for (p, _), t in sorted(getattr(self, "_taps", {}).items()) if p == pond]

    def tap_metrics(self) -> list[dict]:
        """Per-Tap counters for ``/metrics``."""
        with self.lock:
            return [{"name": self.meta[t.pond]["name"], "major": self.meta[t.pond]["major"], "tap": t.name,
                     "queries": t.query_count, "refreshes": t.refresh_count}
                    for t in self._taps.values() if t.pond in self.meta]


def apply_tap(driver, scope: TapScope) -> None:
    """The Tap step every front door runs before answering a query through a Tap (HTTP, the pg and Flight wires).
    Raises KeyError for a Tap that has gone."""
    driver.tap_query(scope)


__all__ = ["TAP_KEY_PREFIX", "Tap", "TapsMixin", "apply_tap", "mint_tap_key"]
