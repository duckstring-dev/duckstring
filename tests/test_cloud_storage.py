"""Azure and Google Cloud Storage as a data root and Spout target, offline (plans/azure-gcp.md, phase 1).

A data root has two halves that must authenticate the same way: fsspec (adlfs, gcsfs) lists, moves and
deletes, and DuckDB (its ``azure`` and ``httpfs`` extensions) reads and writes the Parquet. These tests pin how
each half is configured from one URI and one environment, without a network. The emulator-backed suites
(``test_object_store_azure.py``, ``test_object_store_gcs.py``) run the same paths against a server.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from duckstring import storage
from duckstring.storage import duckdb_secret_sql, get_storage

_AZURE_ENV = ("AZURE_STORAGE_CONNECTION_STRING", "AZURE_STORAGE_ACCOUNT_NAME", "AZURE_STORAGE_ACCOUNT_KEY",
              "AZURE_STORAGE_SAS_TOKEN", "AZURE_STORAGE_TENANT_ID", "AZURE_STORAGE_CLIENT_ID",
              "AZURE_STORAGE_CLIENT_SECRET", "AZURE_STORAGE_ANON")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in _AZURE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(storage, "_google_creds", {})
    yield


# ─── Azure: one credential for both halves ───────────────────────────────────────

_ABFSS = "abfss://lake@acct.dfs.core.windows.net/duckstring"


def test_the_account_comes_from_an_abfss_uri_else_a_parameter_else_the_environment(monkeypatch):
    assert storage.azure_account(_ABFSS, {}) == "acct"
    assert storage.azure_account("az://lake/p", {"account_name": "named"}) == "named"
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", "fromenv")
    assert storage.azure_account("az://lake/p", {}) == "fromenv"


def _adlfs_choice(base: str, params: dict) -> str:
    """Which credential adlfs settles on, given the options the data plane passes it (and the environment)."""
    adlfs = pytest.importorskip("adlfs")

    fs = adlfs.AzureBlobFileSystem(skip_instance_cache=True, **storage._adlfs_options(base, params))
    if fs.connection_string:
        return "connection_string"
    if fs.account_key:
        return "account_key"
    if fs.sas_token:
        return "sas"
    name = type(fs.credential).__name__
    return {"ClientSecretCredential": "service_principal", "DefaultAzureCredential": "chain"}.get(name, name)


def _duckdb_choice(base: str, params: dict) -> str:
    sql = duckdb_secret_sql(base, params, name="t")[1]
    if f"CONNECTION_STRING '{_CS}'" in sql:
        return "connection_string"
    if "SharedAccessSignature=" in sql:
        return "sas"
    if "AccountKey=a2V5" in sql:
        return "account_key"
    return "service_principal" if "service_principal" in sql else "chain"


_CS = "DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=Y29ubg==;EndpointSuffix=core.windows.net"
_SP = {"tenant_id": "t", "client_id": "c", "client_secret": "s"}


@pytest.mark.parametrize("params, env, expected", [
    ({}, {}, "chain"),
    (_SP, {}, "service_principal"),
    ({}, {"AZURE_STORAGE_TENANT_ID": "t", "AZURE_STORAGE_CLIENT_ID": "c", "AZURE_STORAGE_CLIENT_SECRET": "s"},
     "service_principal"),
    ({"sas_token": "sv=1&sig=x"}, {}, "sas"),
    ({"account_key": "a2V5"}, {}, "account_key"),
    ({}, {"AZURE_STORAGE_ACCOUNT_KEY": "a2V5"}, "account_key"),
    ({"connection_string": _CS}, {}, "connection_string"),
    # adlfs's precedence, copied: a connection string beats a key, a key beats a SAS beats a service principal,
    # wherever each came from.
    (_SP, {"AZURE_STORAGE_CONNECTION_STRING": _CS}, "connection_string"),
    ({**_SP, "account_key": "a2V5"}, {}, "account_key"),
    ({**_SP, "sas_token": "sv=1&sig=x"}, {}, "sas"),
    ({"account_key": "a2V5"}, {"AZURE_STORAGE_CONNECTION_STRING": _CS}, "connection_string"),
])
def test_duckdb_and_adlfs_pick_the_same_azure_credential(monkeypatch, params, env, expected):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert _duckdb_choice(_ABFSS, params) == expected
    assert _adlfs_choice(_ABFSS, params) == expected


def test_azure_secrets_are_scoped_to_their_container_and_name_the_account():
    extension, sql = duckdb_secret_sql(_ABFSS, {}, name="duckstring_data")
    assert extension == "azure"
    assert sql == ("CREATE OR REPLACE SECRET duckstring_data (TYPE AZURE, PROVIDER credential_chain, "
                   "ACCOUNT_NAME 'acct', SCOPE ['abfss://lake@acct.dfs.core.windows.net', "
                   "'abfss://acct.dfs.core.windows.net/lake'])")  # the second is the form a glob's files take
    _, sql = duckdb_secret_sql("az://lake/p", {"account_name": "acct", "account_key": "k'ey"}, name="s")
    assert "AccountKey=k''ey" in sql and sql.endswith("SCOPE 'az://lake')")  # quoted, not injectable


def test_azure_credentials_are_resolved_from_references(monkeypatch):
    monkeypatch.setenv("LAKE_KEY", "c2VjcmV0+/=")
    _, sql = duckdb_secret_sql("az://lake/p", {"account_name": "acct", "account_key": "${env:LAKE_KEY}"}, name="s")
    assert "AccountKey=c2VjcmV0+/=" in sql and "${env:" not in sql


@pytest.mark.timeout(120)  # the first use downloads DuckDB's azure extension
@pytest.mark.parametrize("params", [{}, _SP, {"connection_string": _CS}, {"account_key": "a2V5"},
                                    {"sas_token": "sv=1&sig=x"}])
def test_duckdb_accepts_every_azure_secret(params):
    con = duckdb.connect()
    extension, sql = duckdb_secret_sql(_ABFSS, params, name="probe")
    try:
        con.execute(f"INSTALL {extension}; LOAD {extension}")
    except duckdb.Error as exc:  # offline with no cached extension
        pytest.skip(f"DuckDB's {extension} extension is unavailable: {exc}")
    con.execute(sql)
    scope = con.execute("SELECT scope FROM duckdb_secrets() WHERE name = 'probe'").fetchall()[0][0]
    assert scope == ["abfss://lake@acct.dfs.core.windows.net", "abfss://acct.dfs.core.windows.net/lake"]
    # A glob's files come back in the account-host form, and must still find the secret.
    for path in ("abfss://lake@acct.dfs.core.windows.net/t/p.parquet", "abfss://acct.dfs.core.windows.net/lake/t/p.parquet"):
        assert con.execute(f"SELECT name FROM which_secret('{path}', 'azure')").fetchall() == [("probe",)]
    assert con.execute("SELECT name FROM which_secret('abfss://acct.dfs.core.windows.net/other/p.parquet', 'azure')"
                       ).fetchall() == []


def test_adlfs_gets_the_account_from_an_abfss_uri(monkeypatch):
    import fsspec

    captured = {}
    monkeypatch.setenv("SP_SECRET", "s+/=")
    monkeypatch.setattr(fsspec, "filesystem", lambda protocol, **kw: captured.update(protocol=protocol, kw=kw))
    _ = get_storage(_ABFSS + "?client_id=c&tenant_id=t&client_secret=${env:SP_SECRET}").fs
    assert captured["protocol"] == "abfs"
    assert captured["kw"] == {"use_listings_cache": False, "account_name": "acct", "tenant_id": "t",
                              "client_id": "c", "client_secret": "s+/="}


def test_a_missing_extra_names_it(monkeypatch):
    import fsspec

    def missing(protocol, **kw):
        raise ImportError("Install adlfs to access Azure Datalake Gen2 and Azure Blob Storage")

    monkeypatch.setattr(fsspec, "filesystem", missing)
    with pytest.raises(ImportError, match=r"duckstring\[azure\]"):
        _ = get_storage("az://lake/p?account_name=a").fs


# ─── Google Cloud Storage: Google credentials, or HMAC keys for DuckDB alone ─────


class _FakeCreds:
    """Stands in for google-auth credentials: ``refresh`` mints a token living ``life``."""

    def __init__(self, life=timedelta(hours=1)):
        self.token = None
        self.expiry = None
        self.life = life
        self.refreshes = 0

    def refresh(self, request):
        self.refreshes += 1
        self.token = f"token-{self.refreshes}"
        self.expiry = (datetime.now(timezone.utc) + self.life).replace(tzinfo=None)  # naive UTC, as google-auth


@pytest.fixture
def adc(monkeypatch):
    """Application default credentials that need no Google account."""
    google_auth = pytest.importorskip("google.auth")
    creds = _FakeCreds()
    monkeypatch.setattr(google_auth, "default", lambda scopes=None: (creds, "project"))
    return creds


def test_gs_without_keys_gives_duckdb_a_bearer_token_from_google_credentials(adc):
    extension, sql = duckdb_secret_sql("gs://bucket/lake", {}, name="duckstring_data")
    assert extension == "httpfs"
    assert sql == ("CREATE OR REPLACE SECRET duckstring_data (TYPE GCS, BEARER_TOKEN 'token-1', "
                   "SCOPE 'gs://bucket')")
    assert "credential_chain" not in sql  # DuckDB's GCS chain is the AWS one (HMAC keys in AWS_* variables)


def test_a_token_is_reminted_only_when_less_than_forty_minutes_are_left(adc):
    first = storage._token_generation
    assert storage._google_token(None) == "token-1"
    assert storage._google_token(None) == "token-1" and adc.refreshes == 1
    adc.expiry = (datetime.now(timezone.utc) + timedelta(minutes=30)).replace(tzinfo=None)
    assert storage._google_token(None) == "token-2"
    assert storage._token_generation == first + 2


def test_hmac_keys_still_serve_duckdb_and_anon_needs_no_secret():
    _, sql = duckdb_secret_sql("gs://bucket/p", {"key_id": "GOOG1", "secret": "s"}, name="s")
    assert "KEY_ID 'GOOG1'" in sql and "BEARER_TOKEN" not in sql
    assert duckdb_secret_sql("gs://bucket/p", {"credentials": "anon"}, name="s") == ("httpfs", None)


def test_a_service_account_key_reaches_both_halves(monkeypatch):
    service_account = pytest.importorskip("google.oauth2.service_account")
    key = '{"type": "service_account", "private_key": "-----BEGIN+KEY-----\\nabc/=\\n", "client_email": "d@p"}'
    monkeypatch.setenv("GCS_KEY", key)
    seen = {}

    def from_info(info, scopes=None):
        seen["info"], seen["scopes"] = info, scopes
        return _FakeCreds()

    monkeypatch.setattr(service_account.Credentials, "from_service_account_info", from_info)
    _, sql = duckdb_secret_sql("gs://bucket/p", {"credentials": "${env:GCS_KEY}"}, name="s")
    assert "BEARER_TOKEN 'token-1'" in sql
    assert seen["info"]["private_key"] == "-----BEGIN+KEY-----\nabc/=\n"
    assert seen["scopes"] == [storage._GCS_SCOPE]
    assert storage._gcsfs_options({"credentials": "${env:GCS_KEY}"})["token"]["client_email"] == "d@p"


def test_a_bad_service_account_key_is_refused_without_echoing_it():
    with pytest.raises(ValueError, match="service-account key") as exc:
        storage._gcsfs_options({"credentials": "{not json: hunter2"})
    assert "hunter2" not in str(exc.value)


def test_gcsfs_never_gets_hmac_keys(monkeypatch):
    import fsspec

    captured = {}
    monkeypatch.setattr(fsspec, "filesystem", lambda protocol, **kw: captured.update(protocol=protocol, kw=kw))
    _ = get_storage("gs://bucket/p?key_id=GOOG1&secret=s").fs
    assert captured["protocol"] == "gcs"
    assert captured["kw"] == {"use_listings_cache": False}


def test_secrets_for_different_stores_live_side_by_side_on_one_connection(adc):
    """Each bucket or container gets its own secret: another store set up on the same connection, such as the
    Flock's S3 staging prefix, never replaces the data root's."""
    con = duckdb.connect()
    get_storage("gs://lake/duckstring").child("sales", "m1", "data").duckdb_setup(con)
    get_storage("gs://other/x").duckdb_setup(con)
    get_storage("s3://staging/flock?key_id=k&secret=s&region=eu-west-1").duckdb_setup(con)
    scopes = sorted(s[0] for (s,) in con.execute("SELECT scope FROM duckdb_secrets()").fetchall())
    assert scopes == ["gs://lake", "gs://other", "s3://"]


def test_the_credential_generation_moves_with_a_reminted_token(adc):
    assert storage.credential_generation(None) == 0
    assert storage.credential_generation("/srv/data") == 0
    assert storage.credential_generation("gs://bucket/p?key_id=k&secret=s") == 0  # HMAC keys don't expire
    before = storage.credential_generation("gs://bucket/p")
    assert storage.credential_generation("gs://bucket/p") == before
    adc.expiry = (datetime.now(timezone.utc) + timedelta(minutes=5)).replace(tzinfo=None)
    assert storage.credential_generation("gs://bucket/p") == before + 1


def test_the_ops_serving_surface_is_rebuilt_when_the_token_is_reminted(monkeypatch):
    from duckstring.catchment import serving

    class _Driver:
        data_version = 7
        data_root = "gs://bucket/p"

    class _Con:
        def close(self):
            pass

    built = []
    monkeypatch.setattr(serving, "build_serving_con", lambda driver, sandboxed, tap=None: built.append(sandboxed)
                        or _Con())
    generation = [1]
    monkeypatch.setattr(storage, "credential_generation", lambda location: generation[0])
    cache = serving.ServingCache()
    ops, sandbox = cache.connection(_Driver(), sandboxed=False), cache.connection(_Driver(), sandboxed=True)
    assert cache.connection(_Driver(), sandboxed=False) is ops
    generation[0] = 2
    assert cache.connection(_Driver(), sandboxed=False) is not ops  # its views read with the old token
    assert cache.connection(_Driver(), sandboxed=True) is sandbox  # materialised: no credential needed later
    assert built == [False, True, False]
