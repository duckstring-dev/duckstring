"""The provider seam (plans/azure-gcp.md, phase 2): one cloud per Catchment, picked by the data root's scheme,
with remote Ducks only on that cloud. AWS keeps its behaviour; Azure and Google are storage only for now."""

from __future__ import annotations

import logging
import os
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from duckstring.catchment import cloud, cloud_deploy, providers
from duckstring.catchment.db import connect, migrate
from duckstring.catchment.driver import Driver
from duckstring.catchment.launcher import DispatchingLauncher, NoopLauncher, SubprocessLauncher
from duckstring.catchment.providers import aws
from duckstring.catchment.routes import router
from duckstring.catchment.routes.deploy import _register
from duckstring.catchment.secrets import SecretStore

pytestmark = pytest.mark.timeout(10)


@pytest.fixture(autouse=True)
def _aws_creds_from_secrets_only(monkeypatch):
    """AWS credentials come only from the secret store a test passes (never the machine's own profile)."""
    for k in ("AWS_ACCESS_KEY_ID", "AWS_PROFILE", "AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(aws, "_chain_has_credentials", lambda: False)
    monkeypatch.setenv("DUCKSTRING_RELAY", "off")


class _Secrets:
    def __init__(self, *names):
        self._names = names

    def names(self):
        return [{"name": n, "set_at": "t"} for n in self._names]


_AWS = _Secrets("AWS_ACCESS_KEY_ID")


def _driver(tmp_path, data_root=None):
    db = connect(tmp_path / "duck.db")
    migrate(db)
    cfg = {"sources": {}, "immediate_retries": 0, "source_retries": 0, "kind": "inlet"}
    _register(db, "src", "1.0.0", "inlet", "ponds/src/1.0.0", cfg, [{"func": "f1", "name": "r1", "parents": []}])
    return Driver(db, tmp_path, "http://x", NoopLauncher(), data_root=data_root)


# ─── the registry ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("root, name", [
    ("s3://bucket/p", "aws"), ("s3a://bucket/p", "aws"), ("gs://bucket/p", "gcp"), ("gcs://bucket/p", "gcp"),
    ("az://lake/p", "azure"), ("abfss://lake@acct.dfs.core.windows.net/p", "azure"),
    ("abfs://lake@acct.dfs.core.windows.net/p", "azure"),
])
def test_the_data_root_scheme_picks_the_cloud(root, name):
    assert providers.for_data_root(root).name == name


@pytest.mark.parametrize("root", [None, "", "/srv/data", "file:///srv/data", "dbfs:/Volumes/main/lake"])
def test_a_local_root_has_no_cloud(root):
    assert providers.for_data_root(root) is None


def test_backends_belong_to_one_cloud():
    assert providers.all_backends() == ["fargate", "ec2"]
    assert providers.for_backend("fargate").name == "aws" and providers.for_backend("ec2").name == "aws"
    assert providers.for_backend(providers.LOCAL_BACKEND) is None and providers.for_backend("k8s") is None
    assert not providers.get("azure").compute and not providers.get("gcp").compute
    for p in providers.providers().values():
        assert not p.presets or set(p.presets) == set(providers.PRESET_NAMES)


def test_deploy_fields_come_from_the_backends_provider():
    assert cloud_deploy.fields("ec2") == aws.EC2.fields
    assert cloud_deploy.fields(None) == aws.FARGATE.fields  # a NULL pool provider is Fargate
    assert cloud_deploy.fields("local") == () and cloud_deploy.missing_fields("local", None) == []
    assert cloud_deploy.clean("local", {"ami": "x"}) == {}


# ─── the gate: one cloud per Catchment ───────────────────────────────────────────

def test_aws_credentials_no_longer_enable_compute_for_a_gcs_root():
    # The accident phase 2 removes: a gs:// root plus AWS credentials enabled Fargate Ducks, whose image
    # can't read Google storage.
    st = cloud.cloud_status("gs://bucket/p", _AWS)
    assert st["provider"] == "gcp" and st["data_root_remote"] is True and st["compute_available"] is False
    assert st["cloud_enabled"] is False and st["fargate_enabled"] is False and st["backends"] == {}
    assert st["aws_configured"] is True  # still reported, for the UI
    assert not cloud.compute_enabled("gs://bucket/p", _AWS)
    assert not cloud.compute_enabled("abfss://lake@acct.dfs.core.windows.net/p", _AWS)


def test_an_s3_root_with_aws_credentials_enables_its_backends():
    st = cloud.cloud_status("s3://bucket/p", _AWS)
    assert st["provider"] == "aws" and st["provider_title"] == "AWS" and st["compute_available"] is True
    assert st["credentials_configured"] is True and st["cloud_enabled"] is True
    assert set(st["backends"]) == {"fargate", "ec2"}
    assert cloud.cloud_status("s3://bucket/p", _Secrets())["cloud_enabled"] is False
    assert cloud.cloud_status(None, _AWS)["provider"] is None


def test_no_backends_are_built_off_aws(tmp_path):
    from duckstring.catchment.cloud_backends import build_remote_backends

    assert build_remote_backends(tmp_path, "http://x:1", "tok", "gs://bucket/p", _AWS) == ({}, None)
    remotes, _ = build_remote_backends(tmp_path, "http://x:1", "tok", "s3://bucket/p", _AWS)
    assert set(remotes) == {"fargate", "ec2"}


def test_a_switch_to_another_cloud_detaches_the_old_clouds_backends(tmp_path):
    from duckstring.catchment.cloud_backends import refresh_cloud_backends

    store = SecretStore(tmp_path)
    store.set("AWS_ACCESS_KEY_ID", "AKIA_TEST")
    disp = DispatchingLauncher(SubprocessLauncher(tmp_path, "http://x:1"), {})
    app = types.SimpleNamespace(state=types.SimpleNamespace(
        launcher=disp, data_root="s3://bucket/p", secret_store=store, root=tmp_path, duck_token="tok"))
    assert refresh_cloud_backends(app) is True and set(disp.remotes) == {"fargate", "ec2"}
    app.state.data_root = "gs://bucket/p"
    assert refresh_cloud_backends(app) is False
    assert disp.remotes == {}  # spawns go to the Catchment's own machine


def test_a_pool_never_borrows_another_backend():
    disp = DispatchingLauncher(object(), {"fargate": "F"})
    assert disp._remote_for({"pool": {"provider": "fargate"}}) == "F"
    assert disp._remote_for({}) == "F"  # no pool (a dedicated Duck): the default backend
    assert disp._remote_for({"pool": {"provider": "ec2"}}) is None  # before: Fargate took it


# ─── pools on the Catchment's cloud ──────────────────────────────────────────────

def test_a_pool_must_use_a_backend_on_the_catchments_cloud(tmp_path):
    d = _driver(tmp_path, data_root="gs://bucket/p")
    with pytest.raises(ValueError, match="provider must be one of local on Google Cloud"):
        d.add_pool("heavy", provider="fargate", cpu=1024, memory=4096)
    d.add_pool("box", provider="local")
    assert d.get_pool("box")["provider"] == "local"


def test_an_aws_catchment_takes_its_own_backends_only(tmp_path):
    d = _driver(tmp_path, data_root="s3://bucket/p")
    d.add_pool("heavy", provider="ec2", instance_type="m6i.large")
    d.add_pool("light")  # the default backend
    assert d.get_pool("light")["provider"] == "fargate"
    with pytest.raises(ValueError, match="fargate, ec2, local on AWS"):
        d.add_pool("bad", provider="k8s")


def test_a_local_catchment_accepts_any_backend_until_it_has_a_cloud(tmp_path):
    d = _driver(tmp_path)
    d.add_pool("heavy", provider="ec2", instance_type="m6i.large")
    with pytest.raises(ValueError, match="fargate, ec2, local$"):
        d.add_pool("bad", provider="k8s")


def test_a_pool_left_on_another_cloud_runs_on_the_catchment(tmp_path, caplog):
    d = _driver(tmp_path)
    d.add_pool("heavy", provider="fargate", cpu=1024, memory=4096)
    d.set_duck("src@1", duck_target="heavy")
    assert d.duck_config("src@1")["duck_target"] == "heavy"
    d.data_root = "gs://bucket/p"  # as switch_data_root leaves it
    d._pool_cache = None
    with caplog.at_level(logging.WARNING, logger="duckstring.catchment"):
        first = d.duck_config("src@1")
        d.duck_config("src@1")
    assert first["duck_target"] == "catchment" and first["remote"] is False and first["pool"] is None
    warned = [r for r in caplog.records if "pool 'heavy' runs on fargate" in r.getMessage()]
    assert len(warned) == 1 and "Google Cloud" in warned[0].getMessage()


def test_presets_resolve_on_the_catchments_cloud(tmp_path):
    for i, root in enumerate((None, "s3://bucket/p", "gs://bucket/p")):
        (tmp_path / str(i)).mkdir()
        presets = {p["name"]: p for p in _driver(tmp_path / str(i), root).list_pools() if p.get("managed")}
        assert set(presets) == set(providers.PRESET_NAMES)
        assert presets["M"]["provider"] == "fargate" and presets["M"]["memory"] == 4096


# ─── routes ──────────────────────────────────────────────────────────────────────

def test_compute_defaults_list_every_backend(tmp_path):
    app = FastAPI()
    app.include_router(router, prefix="/api")
    d = _driver(tmp_path)
    app.state.driver, app.state.db = d, d.db
    got = TestClient(app).get("/api/catchment/compute-defaults").json()
    assert set(got) == {"fargate", "ec2"} and got["ec2"]["fields"] == list(aws.EC2.fields)


def test_a_storage_only_clouds_secrets_stay_out_of_the_environment(tmp_path, monkeypatch):
    # Until Azure has remote Ducks an AZURE_* secret is an ordinary secret (a Spout's ${secret:} reference):
    # loading it into the environment would change how the data root's adlfs and DuckDB authenticate.
    monkeypatch.delenv("AZURE_CLIENT_SECRET", raising=False)
    from duckstring.catchment.app import create_app

    with TestClient(create_app(tmp_path)) as c:
        assert c.post("/api/secrets", json={"name": "AZURE_CLIENT_SECRET", "value": "s"}).status_code == 200
    assert "AZURE_CLIENT_SECRET" not in os.environ
    cloud.load_env(SecretStore(tmp_path))
    assert "AZURE_CLIENT_SECRET" not in os.environ
