"""MotherDuck conformance: every builder shape, computed locally and on a real MotherDuck account, must give
the same rows (plans/flock-motherduck.md).

MotherDuck runs DuckDB, so this is mostly a check of the plumbing (uploads, the result stream back, version
compatibility between the local DuckDB and MotherDuck's) rather than of semantics. Sources are uploaded,
since a local data root isn't reachable from MotherDuck; reading Sources in place from an object-store root
also needs a ``CREATE SECRET ... IN MOTHERDUCK`` for the bucket, which this test doesn't assume.

Skipped unless ``MOTHERDUCK_TOKEN`` (or ``motherduck_token``) is set. ``DUCKSTRING_FLOCK_MOTHERDUCK_DATABASE``
picks the scratch database (default ``duckstring_flock``, created if missing).
"""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.timeout(600),
    pytest.mark.skipif(not (os.environ.get("MOTHERDUCK_TOKEN") or os.environ.get("motherduck_token")),
                       reason="needs a MotherDuck token (MOTHERDUCK_TOKEN)"),
]


@pytest.fixture
def sources(tmp_path):
    from test_flock_remote import publish_sources  # the same published Sources as the stand-in tests

    return publish_sources(tmp_path)


@pytest.fixture
def motherduck(monkeypatch):
    from duckstring import flock
    from duckstring.flock.engines.motherduck import MotherDuckEngine

    engine = MotherDuckEngine(dict(os.environ))
    assert engine.enabled()
    monkeypatch.setattr(flock, "get_engine", lambda env=None: engine)
    monkeypatch.setattr(flock, "enabled", lambda env=None: True)
    flock._stats.update({"dispatched": 0, "failed": 0, "last_error": None})
    return engine


def test_every_shape_matches_on_motherduck(sources, motherduck):
    from test_flock_remote import SHAPES, _both, _pond

    pond = _pond(sources)
    for name, (build, pk) in sorted(SHAPES.items()):
        local, remote = _both(pond, build, f"md_{name}", pk)
        assert local and local == remote, name


def test_aggregates_and_sql_match_on_motherduck(sources, motherduck):
    from test_flock_remote import test_aggregate_ships_its_accumulators_and_matches_local as aggregate
    from test_flock_remote import test_sql_queries_run_remotely_including_window_functions_and_chains as sql

    aggregate(sources, motherduck)
    sql(sources, motherduck)
