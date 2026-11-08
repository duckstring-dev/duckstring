"""StarRocks conformance: what the StarRocks Flock engine may be trusted with, measured against a real
StarRocks (plans/flock-coverage.md, part 2).

Every candidate construct runs through the engine's real path (translated by sqlglot with the engine's
rewrites, sources staged as Parquet, run on StarRocks, the result read back and conformed) and is compared
with DuckDB computing the same job locally. The data covers the cases where engines usually part ways:
NULLs, zero (and -0.0), negatives, values exactly halfway, large values, empty and padded strings, and
non-ASCII text.

- Every construct the engine **allows** must agree exactly.
- Every construct it **accepts by kind** must agree within that kind's bound.
- Constructs **left out** are recorded with the divergence that keeps them out; one that starts agreeing is
  reported, so the list can grow from evidence.

Needs a StarRocks and an S3 API both can reach (CI: ``starrocks/allin1-ubuntu`` and MinIO on one Docker
network). Skipped unless ``DUCKSTRING_TEST_STARROCKS`` (``host:port``) and ``DUCKSTRING_TEST_S3`` are set;
``DUCKSTRING_TEST_STARROCKS_S3_ENDPOINT`` is the S3 endpoint as StarRocks sees it.

Run ``python tests/test_flock_starrocks_conformance.py`` for the full report.
"""

from __future__ import annotations

import os

import flock_conformance as C
import pytest

pytestmark = [
    pytest.mark.timeout(600),
    pytest.mark.skipif(not (os.environ.get("DUCKSTRING_TEST_STARROCKS") and os.environ.get("DUCKSTRING_TEST_S3")),
                       reason="needs DUCKSTRING_TEST_STARROCKS and DUCKSTRING_TEST_S3"),
]

BUCKET = "duckstring-flock"
KEY = os.environ.get("DUCKSTRING_TEST_S3_KEY", "duckstringtest")
SECRET = os.environ.get("DUCKSTRING_TEST_S3_SECRET", "duckstringtest")


def _env():
    host, _, port = os.environ["DUCKSTRING_TEST_STARROCKS"].partition(":")
    endpoint = os.environ["DUCKSTRING_TEST_S3"].rstrip("/")
    return {
        "DUCKSTRING_FLOCK_STARROCKS_HOST": host, "DUCKSTRING_FLOCK_STARROCKS_PORT": port or "9030",
        "DUCKSTRING_FLOCK_STARROCKS_USER": os.environ.get("DUCKSTRING_TEST_STARROCKS_USER", "root"),
        "DUCKSTRING_FLOCK_STARROCKS_SCRATCH": f"s3://{BUCKET}/scratch?key_id={KEY}&secret={SECRET}&region=us-east-1",
        "DUCKSTRING_FLOCK_STARROCKS_REGION": "us-east-1",
        "DUCKSTRING_FLOCK_STARROCKS_S3_ENDPOINT": os.environ.get("DUCKSTRING_TEST_STARROCKS_S3_ENDPOINT", endpoint),
        "STARROCKS_S3_ACCESS_KEY": KEY, "STARROCKS_S3_SECRET_KEY": SECRET,
        "DUCKSTRING_S3_ENDPOINT": endpoint,
    }


def _setup(tmp_path, dims=False):
    import boto3

    env = _env()
    os.environ["DUCKSTRING_S3_ENDPOINT"] = env["DUCKSTRING_S3_ENDPOINT"]
    s3 = boto3.client("s3", endpoint_url=env["DUCKSTRING_S3_ENDPOINT"], region_name="us-east-1",
                      aws_access_key_id=KEY, aws_secret_access_key=SECRET)
    try:
        s3.create_bucket(Bucket=BUCKET)
    except Exception:  # noqa: BLE001 (already there)
        pass
    from duckstring.flock.engines.starrocks import StarRocksEngine

    return C.make_pond(tmp_path, dims=dims), StarRocksEngine(env)


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    return _setup(tmp_path_factory.mktemp("sr"))


@pytest.mark.parametrize("kind,label,expr", C.CANDIDATES, ids=[c[1] for c in C.CANDIDATES])
def test_every_allowed_candidate_agrees(setup, kind, label, expr):
    """A candidate the engine would send (its constructs all allowed, or accepted as float_order) must agree;
    one it refuses is skipped here and listed by the report."""
    from duckstring.flock.engines.starrocks import PROFILE

    pond, engine = setup
    reason = C.sent(pond, PROFILE, kind, expr)
    if reason is not None:
        pytest.skip(f"not sent: {reason}")
    problem = C.candidate_agrees(pond, engine, PROFILE, kind, label, expr)
    assert problem is None, problem


@pytest.fixture(scope="module")
def shape_setup(tmp_path_factory):
    return _setup(tmp_path_factory.mktemp("srshapes"), dims=True)


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

    from duckstring.flock.engines.starrocks import PROFILE

    pond, engine = _setup(Path(tempfile.mkdtemp()))
    C.report(pond, engine, PROFILE)
