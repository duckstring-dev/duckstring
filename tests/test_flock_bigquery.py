"""The BigQuery Flock engine, offline (plans/databricks-bigquery.md, part 1): its jobs over REST (insert, poll,
errors, cancel on timeout), the statements it sends, and its translation rewrites. What BigQuery is trusted with is
measured in ``test_flock_bigquery_conformance.py``."""

from __future__ import annotations

import pytest

from duckstring import flock

pytest.importorskip("sqlglot")
pytestmark = pytest.mark.timeout(10)


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.content = b"x" if body is not None else b""
        self.text = str(body)

    def json(self):
        return self._body


class FakeSession:
    """Answers jobs.insert with a RUNNING job, then DONE after ``polls`` GETs (or with ``error``)."""

    def __init__(self, polls=1, error=None):
        self.polls, self.error = polls, error
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, timeout=None, **kwargs):
        self.calls.append((method, url, kwargs))
        ref = {"projectId": "p", "jobId": "j1", "location": "australia-southeast1"}
        if method == "POST" and url.endswith("/jobs"):
            return FakeResponse(200, {"jobReference": ref, "status": {"state": "RUNNING"}})
        if method == "POST" and url.endswith("/cancel"):
            return FakeResponse(200, {})
        self.polls -= 1
        if self.polls > 0:
            return FakeResponse(200, {"jobReference": ref, "status": {"state": "RUNNING"}})
        status = {"state": "DONE", **({"errorResult": {"message": self.error}} if self.error else {})}
        return FakeResponse(200, {"jobReference": ref, "status": status,
                                  "statistics": {"query": {"totalBytesBilled": "10485760"}}})


def _engine(session, **env):
    from duckstring.flock.engines.bigquery import BigQueryEngine

    e = BigQueryEngine({"DUCKSTRING_FLOCK_BIGQUERY_DATASET": "flock", "DUCKSTRING_FLOCK_BIGQUERY_SCRATCH":
                        "gs://bucket/scratch", **env})
    e._session_cache = (session, "p")
    return e


def test_a_statement_is_polled_to_completion_in_its_location():
    s = FakeSession(polls=3)
    stats = _engine(s)._query("SELECT 1")
    assert stats["query"]["totalBytesBilled"] == "10485760"
    gets = [c for c in s.calls if c[0] == "GET"]
    assert len(gets) == 3 and all(c[2]["params"] == {"location": "australia-southeast1"} for c in gets)


def test_a_failed_job_raises_with_bigquerys_message():
    from duckstring.flock.engines.bigquery import BigQueryError

    with pytest.raises(BigQueryError, match="Division by zero"):
        _engine(FakeSession(error="Division by zero: 1 / 0"))._query("SELECT 1/0")


def test_a_job_past_the_timeout_is_cancelled():
    s = FakeSession(polls=10**6)
    e = _engine(s, DUCKSTRING_FLOCK_BIGQUERY_TIMEOUT="0")
    with pytest.raises(TimeoutError):
        e._query("SELECT 1")
    assert s.calls[-1][1].endswith("/jobs/j1/cancel")


def test_sources_are_external_tables_and_results_an_export():
    s = FakeSession()
    e = _engine(s)
    table = e.read_of("gs://bucket/scratch/stage/t1/src0", {"a": "BIGINT"}, "t1", 0)
    assert table == "`p.flock.dsjob_t1_src0`"
    e.execute("SELECT 1 AS a", "gs://bucket/scratch/stage/t1/out/", "t1")
    sent = [c[2]["json"]["configuration"]["query"]["query"] for c in s.calls if c[1].endswith("/jobs")]
    assert sent[0].startswith("CREATE EXTERNAL TABLE `p.flock.dsjob_t1_src0`")
    assert "uris = ['gs://bucket/scratch/stage/t1/src0/data.parquet']" in sent[0]
    assert sent[1].startswith("EXPORT DATA OPTIONS (uri = 'gs://bucket/scratch/stage/t1/out/*.parquet'")
    e._storage = lambda: type("S", (), {"rmtree": lambda *_a: None})()
    e.cleanup("t1")
    last = [c[2]["json"]["configuration"]["query"]["query"] for c in s.calls if c[1].endswith("/jobs")][-1]
    assert last == "DROP EXTERNAL TABLE IF EXISTS `p.flock.dsjob_t1_src0`"


def test_division_and_integer_casts_follow_duckdb():
    import sqlglot
    from sqlglot import exp
    from sqlglot.optimizer.annotate_types import annotate_types

    from duckstring.flock.engines.bigquery import _rewrite

    tree = annotate_types(sqlglot.parse_one("SELECT a / b AS q, CAST(CAST(x AS DOUBLE) AS INT) AS c FROM t",
                                            read="duckdb"))
    sql = _rewrite(tree, exp).sql("bigquery")
    assert "IEEE_DIVIDE" in sql.upper() and "ROUND" in sql.upper()


def test_the_engine_is_registered_and_enabled_by_dataset_and_scratch():
    from duckstring.flock.engines.bigquery import BigQueryEngine

    assert not BigQueryEngine({}).enabled()
    assert flock.default_engine({"DUCKSTRING_FLOCK_BIGQUERY_DATASET": "d",
                                 "DUCKSTRING_FLOCK_BIGQUERY_SCRATCH": "gs://b/s"}) == "bigquery"
