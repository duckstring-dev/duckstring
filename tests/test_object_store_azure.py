"""An Azure data root and Spouts against Azurite (plans/azure-gcp.md, phase 1).

Both halves run against the one emulator: fsspec (adlfs) for listing, sidecars and deletes, and DuckDB's
``azure`` extension for the Parquet. Credentials come from ``AZURE_STORAGE_CONNECTION_STRING``, the variable
both read, so the environment path is what's exercised.

Runs when ``DUCKSTRING_TEST_AZURE`` names Azurite's blob endpoint (``http://127.0.0.1:10000/devstoreaccount1``;
CI starts one), else skips. Azurite has no Data Lake (``dfs``) endpoint, so ``abfss://`` is left to the
real-account smoke test; ``az://`` covers the shared code.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.timeout(120)

_CONTAINER = "duckstring-test"
# Azurite's well-known development account (published in Azure's own docs; not a secret).
_ACCOUNT = "devstoreaccount1"
_KEY = "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="


@pytest.fixture(scope="module")
def azurite():
    endpoint = os.environ.get("DUCKSTRING_TEST_AZURE")
    if not endpoint:
        pytest.skip("set DUCKSTRING_TEST_AZURE to Azurite's blob endpoint (CI starts one)")
    blob = pytest.importorskip("azure.storage.blob")
    connection = (f"DefaultEndpointsProtocol=http;AccountName={_ACCOUNT};AccountKey={_KEY};"
                  f"BlobEndpoint={endpoint.rstrip('/')};")
    service = blob.BlobServiceClient.from_connection_string(connection)
    try:
        service.create_container(_CONTAINER)
    except Exception:  # already there
        pass
    return connection, service.get_container_client(_CONTAINER)


@pytest.fixture
def azure_env(azurite, monkeypatch):
    connection, container = azurite
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", connection)
    return container


def _names(container, prefix: str) -> list[str]:
    return sorted(b.name for b in container.list_blobs(name_starts_with=prefix))


def test_storage_round_trips_through_azure(azure_env, tmp_path):
    from duckstring.storage import get_storage

    store = get_storage(f"az://{_CONTAINER}/roundtrip")
    store.write_text('{"a": 1}', "_trickle.json")
    local = tmp_path / "part.parquet"
    local.write_bytes(b"y")
    store.put_file(local, "t", "p1.parquet")

    assert store.read_text("_trickle.json") == '{"a": 1}' and store.read_text("missing.json") is None
    assert store.parquet_names("t") == ["p1.parquet"] and store.subdir_names() == ["t"]
    assert "roundtrip/t/p1.parquet" in _names(azure_env, "roundtrip/")
    store.rmtree("t")
    assert store.subdir_names() == []


def test_duckdb_writes_and_reads_azure(azure_env):
    import duckdb

    from duckstring.storage import get_storage

    store = get_storage(f"az://{_CONTAINER}/duckdb")
    con = duckdb.connect()
    store.duckdb_setup(con)
    big = "SELECT range AS id, md5(random()::varchar) || md5(random()::varchar) AS pad FROM range(400000)"
    con.execute(f"COPY ({big}) TO '{store.uri('big.parquet')}' (FORMAT PARQUET)")  # several staged blocks
    con.execute(f"COPY (SELECT range AS id FROM range(100)) TO '{store.uri('chunks')}' "
                "(FORMAT PARQUET, FILE_SIZE_BYTES 64)")
    assert con.sql(f"SELECT count(*) FROM read_parquet('{store.uri('big.parquet')}')").fetchall() == [(400000,)]
    assert store.parquet_names("chunks")  # fsspec sees what DuckDB wrote
    assert con.sql(f"SELECT sum(id) FROM read_parquet('{store.glob('*.parquet', 'chunks')}')").fetchall() == [
        (4950,)]


def test_versioned_overwrite_on_azure(azure_env):
    """The data plane end to end on a container: each publish a new version, a pinned read, retention."""
    import duckdb

    from duckstring.dataplane import ParquetDataPlane, prune_versions
    from duckstring.storage import get_storage
    from duckstring.trickle.io import part_name

    store = get_storage(f"az://{_CONTAINER}/versions")
    dp = ParquetDataPlane()
    f1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    f2 = f1 + timedelta(hours=1)
    for f, value, retain in ((f1, 1, None), (f2, 2, f1)):
        con = duckdb.connect()
        con.execute(f"CREATE TABLE a AS SELECT {value} AS v")
        dp.export(con, store, f=f, retain_from=retain)
        con.close()
    assert _names(azure_env, "versions/a__v/") == [f"versions/a__v/{part_name(f1)}",
                                                   f"versions/a__v/{part_name(f2)}"]
    con = duckdb.connect()
    store.duckdb_setup(con)
    assert con.sql(dp.read_select(store, "a", pin=f1)).fetchall() == [(1,)]
    assert con.sql(dp.read_select(store, "a")).fetchall() == [(2,)]
    prune_versions(store, None)
    assert _names(azure_env, "versions/a__v/") == [f"versions/a__v/{part_name(f2)}"]


def test_a_spout_delivers_to_azure(azure_env, tmp_path):
    """Both Spout modes to a container: DuckDB's snapshot and the fsspec mirror of the published layout."""
    import duckdb

    from duckstring import trickle_io as T
    from duckstring.catchment.egress_worker import _egress_spout
    from duckstring.catchment.registry import pond_data_dir
    from duckstring.dataplane import ParquetDataPlane
    from duckstring.storage import get_storage

    f = datetime(2026, 6, 1, tzinfo=timezone.utc)
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.execute("CREATE TABLE revenue AS SELECT * FROM (VALUES (1, 10), (2, 20)) t(id, amt)")
    T.merge_table(con, "fact", con.sql("SELECT * FROM (VALUES (1, 'a'), (2, 'b')) t(id, v)"), f, ("id",))
    ParquetDataPlane().export(con, pond_data_dir(tmp_path, "sales", 1).root, f=f)

    creds = "connection_string=${env:AZURE_STORAGE_CONNECTION_STRING}"
    job = {"pond_name": "sales", "major": 1, "f": f}
    _egress_spout(tmp_path, {**job, "table": "revenue", "mode": "full",
                             "destination": f"az://{_CONTAINER}/spout-full?{creds}"})
    reader = duckdb.connect()
    get_storage(f"az://{_CONTAINER}/spout-full").duckdb_setup(reader)
    rows = reader.sql(f"SELECT * FROM read_parquet('az://{_CONTAINER}/spout-full/revenue.parquet') ORDER BY id")
    assert rows.fetchall() == [(1, 10), (2, 20)]

    _egress_spout(tmp_path, {**job, "table": "fact", "mode": "append",
                             "destination": f"az://{_CONTAINER}/spout-mirror?{creds}"})
    mirror = get_storage(f"az://{_CONTAINER}/spout-mirror")
    mirror.duckdb_setup(reader)
    read = ParquetDataPlane().read_select(mirror, "fact")
    assert sorted(reader.sql(f"SELECT id, v FROM ({read})").fetchall()) == [(1, "a"), (2, "b")]
