"""A ``gs://`` data root and Spouts against servers (plans/azure-gcp.md, phase 1).

Two halves, two stand-ins, since no one emulator serves both:

- **DuckDB** (reads and writes Parquet) speaks GCS's XML API, which is S3's. It runs against an in-process
  moto with a GCS secret pointed at it: the bearer token minted from (fake) Google credentials travels on every
  request and multipart uploads complete. Always runs (moto is in the dev extra). What it can't show is that
  Google's own server accepts the token: that is DuckDB's contract with Google, checked by the real-account
  smoke test.
- **fsspec** (gcsfs: listing, sidecars, leases, deletes) speaks the JSON API. It runs against fake-gcs-server
  when ``DUCKSTRING_TEST_GCS`` names one (CI starts it), else skips.
"""

from __future__ import annotations

import os
import socket
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.timeout(120)

_BUCKET = "duckstring-gcs"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakeCreds:
    def __init__(self):
        self.token, self.expiry, self.refreshes = None, None, 0

    def refresh(self, request):
        self.refreshes += 1
        self.token = f"ya29.fake-{self.refreshes}"
        self.expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None)


@pytest.fixture(scope="module")
def xml_api():
    """moto as a stand-in for GCS's XML API (DuckDB's half)."""
    pytest.importorskip("moto.server")
    boto3 = pytest.importorskip("boto3")
    from moto.server import ThreadedMotoServer

    port = _free_port()
    server = ThreadedMotoServer(port=port, verbose=False)
    server.start()
    endpoint = f"http://127.0.0.1:{port}"
    boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1", aws_access_key_id="x",
                 aws_secret_access_key="y").create_bucket(Bucket=_BUCKET)
    yield endpoint
    server.stop()


@pytest.fixture
def google(monkeypatch):
    """Application default credentials that need no Google account; a fresh token cache."""
    google_auth = pytest.importorskip("google.auth")
    from duckstring import storage

    creds = _FakeCreds()
    monkeypatch.setattr(google_auth, "default", lambda scopes=None: (creds, "project"))
    monkeypatch.setattr(storage, "_google_creds", {})
    return creds


def test_duckdb_writes_and_reads_gs_with_a_token_from_google_credentials(xml_api, google):
    import duckdb

    from duckstring.storage import get_storage

    store = get_storage(f"gs://{_BUCKET}/lake?endpoint={xml_api}")
    con = duckdb.connect()
    store.duckdb_setup(con)
    big = "SELECT range AS id, md5(random()::varchar) || md5(random()::varchar) AS pad FROM range(400000)"
    con.execute(f"COPY ({big}) TO '{store.uri('big.parquet')}' (FORMAT PARQUET)")  # large enough for multipart
    con.execute(f"COPY (SELECT range AS id FROM range(100)) TO '{store.uri('t__v')}' "
                "(FORMAT PARQUET, FILE_SIZE_BYTES 64)")  # a chunked directory, as a merge base is written
    assert con.sql(f"SELECT count(*), sum(id) FROM read_parquet('{store.uri('big.parquet')}')").fetchall() == [
        (400000, 79999800000)]
    assert con.sql(f"SELECT count(*) FROM read_parquet('{store.glob('*.parquet', 't__v')}')").fetchall() == [(100,)]
    assert google.refreshes == 1  # minted once, reused while it has more than 40 minutes left


def test_a_snapshot_spout_delivers_to_gs_with_google_credentials(xml_api, google, tmp_path):
    import duckdb

    from duckstring.catchment.egress_worker import _egress_spout
    from duckstring.catchment.registry import pond_data_dir
    from duckstring.dataplane import ParquetDataPlane
    from duckstring.storage import get_storage

    f = datetime(2026, 6, 1, tzinfo=timezone.utc)
    con = duckdb.connect()
    con.execute("CREATE TABLE revenue AS SELECT * FROM (VALUES (1, 10), (2, 20)) t(id, amt)")
    ParquetDataPlane().export(con, pond_data_dir(tmp_path, "sales", 1).root, f=f)
    _egress_spout(tmp_path, {"pond_name": "sales", "major": 1, "f": f, "table": "revenue", "mode": "full",
                             "destination": f"gs://{_BUCKET}/spout?endpoint={xml_api}"})
    reader = duckdb.connect()
    get_storage(f"gs://{_BUCKET}/spout?endpoint={xml_api}").duckdb_setup(reader)
    rows = reader.sql(f"SELECT * FROM read_parquet('gs://{_BUCKET}/spout/revenue.parquet') ORDER BY id")
    assert rows.fetchall() == [(1, 10), (2, 20)]


# ─── the fsspec half, against fake-gcs-server ───────────────────────────────────


@pytest.fixture(scope="module")
def json_api():
    endpoint = os.environ.get("DUCKSTRING_TEST_GCS")
    if not endpoint:
        pytest.skip("set DUCKSTRING_TEST_GCS to a fake-gcs-server (CI starts one)")
    gcsfs = pytest.importorskip("gcsfs")
    fs = gcsfs.GCSFileSystem(token="anon", endpoint_url=endpoint, skip_instance_cache=True)
    try:
        fs.mkdir(_BUCKET)
    except Exception:  # already there
        pass
    return endpoint.rstrip("/")


def test_storage_round_trips_through_gcs(json_api, tmp_path):
    from duckstring.storage import get_storage

    store = get_storage(f"gs://{_BUCKET}/roundtrip?credentials=anon&endpoint={json_api}")
    store.write_text('{"a": 1}', "_trickle.json")
    store.write_bytes(b"x", "t__v", "2026-01-01T00:00:00+00:00.parquet")
    local = tmp_path / "part.parquet"
    local.write_bytes(b"y")
    store.put_file(local, "t", "p1.parquet")

    assert store.read_text("_trickle.json") == '{"a": 1}' and store.read_text("missing.json") is None
    assert store.parquet_names("t__v") == ["2026-01-01T00:00:00+00:00.parquet"]
    assert store.subdir_names() == ["t", "t__v"]
    assert store.size("t", "p1.parquet") == 1
    store.rmtree("t")
    store.remove("_trickle.json")
    assert store.subdir_names() == ["t__v"] and not store.exists("_trickle.json")


def test_the_single_writer_lease_holds_on_gcs(json_api):
    from duckstring.catchment.data_lease import LeaseConflict, acquire_lease, release_lease
    from duckstring.storage import get_storage

    store = get_storage(f"gs://{_BUCKET}/leased?credentials=anon&endpoint={json_api}")
    acquire_lease(store, "catchment-a")
    with pytest.raises(LeaseConflict):
        acquire_lease(store, "catchment-b", force=False)
    release_lease(store, "catchment-a")
    acquire_lease(store, "catchment-b", force=False)
