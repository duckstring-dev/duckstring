"""BigQuery conformance: what the BigQuery Flock engine may be trusted with, measured against a real BigQuery
(plans/databricks-bigquery.md, part 1). The battery is shared with the other SQL engines (``flock_conformance``).

- Every construct the engine **allows** must agree exactly.
- Every construct it **accepts by kind** must agree within that kind's bound.
- Constructs **left out** are recorded with the divergence that keeps them out; one that starts agreeing is
  reported, so the list can grow from evidence.

Needs a BigQuery dataset and a ``gs://`` scratch prefix in the same location, and Google credentials (application
default credentials, or ``credentials=`` on the scratch URI). Skipped unless both are set::

    DUCKSTRING_FLOCK_BIGQUERY_DATASET=duckstring_flock \\
    DUCKSTRING_FLOCK_BIGQUERY_SCRATCH=gs://…/flock-scratch \\
    DUCKSTRING_FLOCK_BIGQUERY_PROJECT=… \\
    pytest tests/test_flock_bigquery_conformance.py

Run ``python tests/test_flock_bigquery_conformance.py`` for the full report.
"""

from __future__ import annotations

import os

import flock_conformance as C
import pytest

_REQUIRED = ("DUCKSTRING_FLOCK_BIGQUERY_DATASET", "DUCKSTRING_FLOCK_BIGQUERY_SCRATCH")

pytestmark = [
    pytest.mark.timeout(1800),
    pytest.mark.skipif(not all(os.environ.get(k) for k in _REQUIRED),
                       reason="needs DUCKSTRING_FLOCK_BIGQUERY_DATASET and DUCKSTRING_FLOCK_BIGQUERY_SCRATCH"),
]


def _setup(tmp_path, dims=False):
    pytest.importorskip("google.auth")
    from duckstring.flock.engines.bigquery import BigQueryEngine

    env = {k: v for k, v in os.environ.items() if k.startswith("DUCKSTRING_FLOCK_BIGQUERY_")}
    return C.make_pond(tmp_path, dims=dims), BigQueryEngine(env)


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    return _setup(tmp_path_factory.mktemp("bq"))


@pytest.mark.parametrize("kind,label,expr", C.CANDIDATES, ids=[c[1] for c in C.CANDIDATES])
def test_every_allowed_candidate_agrees(setup, kind, label, expr):
    """A candidate the engine would send must agree; one it refuses is skipped here and listed by the report."""
    from duckstring.flock.engines.bigquery import PROFILE

    pond, engine = setup
    reason = C.sent(pond, PROFILE, kind, expr)
    if reason is not None:
        pytest.skip(f"not sent: {reason}")
    problem = C.candidate_agrees(pond, engine, PROFILE, kind, label, expr)
    assert problem is None, problem


@pytest.fixture(scope="module")
def shape_setup(tmp_path_factory):
    return _setup(tmp_path_factory.mktemp("bqshapes"), dims=True)


@pytest.mark.parametrize("shape", C.SHAPES)
def test_builder_jobs_are_sent_and_agree(shape_setup, shape):
    pond, engine = shape_setup
    C.shape_agrees(pond, engine, shape)


def test_float_order_is_needed_for_a_floating_point_aggregate(shape_setup):
    pond, engine = shape_setup
    C.float_order_needed(pond, engine)


if __name__ == "__main__":  # the full report
    import tempfile
    from pathlib import Path

    from duckstring.flock.engines.bigquery import PROFILE

    pond, engine = _setup(Path(tempfile.mkdtemp()))
    C.report(pond, engine, PROFILE)
