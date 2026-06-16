"""Phase 2 (min_version) of plans/data-plane-iceberg.md: the version contract is enforced at deploy.
A Sink can't deploy against a Source selected below its pin, and a Source can't be selected below an
existing downstream pin — except via a major bump, the sanctioned breaking-change escape hatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from duckstring.catchment.db import connect, migrate
from duckstring.catchment.routes.deploy import _register


def _db():
    con = connect(Path(":memory:"))
    migrate(con)
    return con


def _deploy(db, name, version, sources=None, kind="pond"):
    cfg = {"sources": sources or {}, "immediate_retries": 0, "source_retries": 0, "kind": kind}
    _register(db, name, version, kind, f"ponds/{name}/{version}", cfg, [])


def test_sink_may_deploy_before_its_source():
    db = _db()
    # No 'src' deployed yet — the pin is unenforceable, so the Sink deploys (the Source's own deploy
    # re-checks). This preserves "a sink can deploy before its source".
    _deploy(db, "snk", "1.0.0", sources={"src": "1.2.0"})


def test_under_pinned_sink_is_rejected():
    db = _db()
    _deploy(db, "src", "1.0.0")
    with pytest.raises(ValueError, match="1.2.0"):
        _deploy(db, "snk", "1.0.0", sources={"src": "1.2.0"})


def test_sink_deploys_once_source_meets_pin():
    db = _db()
    _deploy(db, "src", "1.2.0")
    _deploy(db, "snk", "1.0.0", sources={"src": "1.2.0"})  # selected 1.2.0 >= pin 1.2.0 → ok


def test_source_downgrade_below_downstream_pin_is_rejected():
    db = _db()
    _deploy(db, "src", "1.2.0")
    _deploy(db, "snk", "1.0.0", sources={"src": "1.2.0"})
    with pytest.raises(ValueError, match="break"):
        _deploy(db, "src", "1.1.0")  # regresses below the downstream pin


def test_additive_newer_source_is_allowed():
    db = _db()
    _deploy(db, "src", "1.2.0")
    _deploy(db, "snk", "1.0.0", sources={"src": "1.2.0"})
    _deploy(db, "src", "1.3.0")  # newer than the pin → fine


def test_major_bump_is_the_escape_hatch():
    db = _db()
    _deploy(db, "src", "1.2.0")
    _deploy(db, "snk", "1.0.0", sources={"src": "1.2.0"})
    # A new major line is independent of the major-1 pin: a breaking change ships here without
    # violating the existing Sink (which keeps consuming major 1).
    _deploy(db, "src", "2.0.0")
