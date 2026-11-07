"""The Container Apps Duck backend and the Azure provider (plans/azure-gcp.md, phase 3), against fake clients
shaped like the responses a real environment gave (2026-11-07)."""

from __future__ import annotations

import types

import pytest

from duckstring.catchment import cloud, cloud_deploy, providers
from duckstring.catchment.containerapps_launcher import ContainerAppsLauncher, _resource_part
from duckstring.catchment.dialback import RemoteDialback
from duckstring.catchment.providers import azure

pytestmark = pytest.mark.timeout(10)

SUB = "00000000-0000-0000-0000-000000000001"
ENV = f"/subscriptions/{SUB}/resourceGroups/lake-rg/providers/Microsoft.App/managedEnvironments/ducks"
IDN = f"/subscriptions/{SUB}/resourceGroups/lake-rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/duck"


class _Poller:
    def __init__(self, value=None):
        self.value = value

    def result(self, timeout=None):
        return self.value


class _Jobs:
    def __init__(self, client):
        self.c = client

    def begin_create_or_update(self, rg, name, body):
        self.c.calls.append(("create", rg, name, body))
        # ARM returns the identity's key in its own casing, with the client id filled in.
        key = IDN.replace("resourceGroups", "resourcegroups")
        return _Poller(types.SimpleNamespace(identity=types.SimpleNamespace(
            user_assigned_identities={key: types.SimpleNamespace(client_id="client-1")})))

    def begin_start(self, rg, job, template=None):
        self.c.calls.append(("start", rg, job, template))
        self.c.n += 1
        return _Poller(types.SimpleNamespace(name=f"{job}-exec{self.c.n}"))

    def begin_stop_execution(self, rg, job, name):
        self.c.calls.append(("stop", rg, job, name))
        return _Poller()


class _Client:
    def __init__(self, status="Running", fail_start=None):
        self.calls, self.n, self.status, self.fail_start = [], 0, status, fail_start
        self.jobs = _Jobs(self)
        if fail_start:
            self.jobs.begin_start = lambda *a, **k: (_ for _ in ()).throw(fail_start)
        logs = types.SimpleNamespace(log_analytics_configuration=types.SimpleNamespace(customer_id="ws-1"))
        self.managed_environments = types.SimpleNamespace(
            get=lambda rg, name: types.SimpleNamespace(location="Australia East", app_logs_configuration=logs))

    def job_execution(self, rg, job, name):
        return types.SimpleNamespace(status=types.SimpleNamespace(value=self.status))


class _Logs:
    def __init__(self, rows, events=()):
        self.rows, self.events, self.queries = rows, list(events), []

    def query_workspace(self, workspace, query, timespan=None):
        self.queries.append((workspace, query))
        rows = self.events if query.startswith("ContainerAppSystemLogs_CL") else self.rows
        return types.SimpleNamespace(tables=[types.SimpleNamespace(rows=rows)])


def _launcher(client=None, logs=None, **kw):
    kw.setdefault("image", "ghcr.io/duckstring-dev/duckstring:0.8.0-azure")
    kw.setdefault("environment", ENV)
    kw.setdefault("identity", IDN)
    return ContainerAppsLauncher("/tmp/x", "http://cat:7474", token="tok", data_root="abfss://c@a.dfs.core.windows.net/d",
                                 dialback=RemoteDialback("http://cat:7474"), client=client or _Client(),
                                 logs_client=logs, **kw)


def _calls(client, kind):
    return [c for c in client.calls if c[0] == kind]


def test_a_duck_is_an_execution_of_a_job_made_once_per_config():
    client = _Client()
    la = _launcher(client)
    la.ensure("sales@1", "1.0.0", "ponds/sales/1.0.0", duck={"pool": {"cpu": 2048, "memory": 4096}})
    la.ensure("reports@1", "1.0.0", "ponds/reports/1.0.0", duck={})
    [create] = _calls(client, "create")  # one job for both Ponds
    _, rg, job, body = create
    assert rg == "lake-rg" and job.startswith("duckstring-duck-") and len(job) <= 32
    props = body["properties"]
    assert body["location"] == "Australia East" and props["environmentId"] == ENV
    assert body["identity"] == {"type": "UserAssigned", "userAssignedIdentities": {IDN: {}}}
    assert props["configuration"]["triggerType"] == "Manual" and props["configuration"]["replicaTimeout"] == 7 * 86400
    assert props["workloadProfileName"] == "Consumption" and "registries" not in props["configuration"]
    starts = _calls(client, "start")
    container = starts[0][3]["containers"][0]
    assert container["args"][:3] == ["duckstring.duck", "--pond", "sales"]
    assert "--catchment" in container["args"] and "--token=tok" in container["args"]
    assert "--data-root=abfss://c@a.dfs.core.windows.net/d" in container["args"]
    assert container["env"] == [{"name": "AZURE_CLIENT_ID", "value": "client-1"}]  # the identity, by case-blind key
    assert container["resources"] == {"cpu": 2.0, "memory": "4Gi"}
    assert starts[1][3]["containers"][0]["resources"] == {"cpu": 1.0, "memory": "2Gi"}  # the default size
    assert la.is_running("sales@1") and la.is_running("reports@1")


def test_an_acr_image_is_pulled_with_the_identity():
    client = _Client()
    _launcher(client, image="lake.azurecr.io/duckstring:dev").ensure("sales@1", "1.0.0", "p", duck={})
    config = _calls(client, "create")[0][3]["properties"]["configuration"]
    assert config["registries"] == [{"server": "lake.azurecr.io", "identity": IDN}]


def test_missing_config_fails_the_launch_with_the_reason():
    client = _Client()
    la = _launcher(client, environment="", identity="")
    la.ensure("sales@1", "1.0.0", "p", duck={})
    assert "DUCKSTRING_CONTAINERAPPS_ENVIRONMENT" in la.launch_error("sales@1")
    assert client.calls == [] and not la.is_running("sales@1")


def test_a_failed_start_is_reported():
    la = _launcher(_Client(fail_start=RuntimeError("QuotaExceeded")))
    la.ensure("sales@1", "1.0.0", "p", duck={})
    assert "QuotaExceeded" in la.launch_error("sales@1") and not la.is_running("sales@1")


def test_a_pools_deploy_config_overrides_the_environment():
    client = _Client()
    other = ENV.replace("/ducks", "/big")
    _launcher(client).ensure("sales@1", "1.0.0", "p", duck={"pool": {"deploy_config": {
        "environment": other, "workload_profile": "D8", "job": "heavy-ducks"}}})
    _, _, job, body = _calls(client, "create")[0]
    assert job == "heavy-ducks" and body["properties"]["environmentId"] == other
    assert body["properties"]["workloadProfileName"] == "D8"


def test_terminate_stops_and_release_forgets():
    client = _Client()
    la = _launcher(client)
    la.ensure("sales@1", "1.0.0", "p", duck={})
    la.ensure("reports@1", "1.0.0", "p", duck={})
    la.release("reports@1")
    assert not la.is_running("reports@1") and _calls(client, "stop") == []
    la.terminate("sales@1")
    [stop] = _calls(client, "stop")
    assert stop[3].endswith("-exec1") and not la.is_running("sales@1")


def test_a_spawn_waits_for_a_reachable_catchment():
    client = _Client()
    la = ContainerAppsLauncher("/tmp/x", None, token="t", image="i", environment=ENV, identity=IDN,
                               dialback=RemoteDialback(None), client=client)
    la.ensure("sales@1", "1.0.0", "p", duck={})
    assert la.is_running("sales@1") and client.calls == []
    la.set_base_url("http://cat:7474")
    assert _calls(client, "start")


def test_diagnose_reports_the_status_and_the_log_tail():
    # Rows as the real workspace returns them: system events by job and execution name, newest first.
    logs = _Logs([("t2", "Traceback: boom"), ("t1", "GET /api/duck/sales/1/jobs 200 OK")],
                 events=[("t2", "ProcessExited", "exit code: 1"), ("t1", "PulledImage", "Successfully pulled image")])
    la = _launcher(_Client(status="Failed"), logs)
    la.ensure("sales@1", "1.0.0", "p", duck={})
    got = la.diagnose("sales@1")
    assert "-exec1 failed" in got and "Traceback: boom" in got and "/jobs" not in got
    assert got.index("PulledImage") < got.index("ProcessExited: exit code: 1") < got.index("Traceback")
    assert {q[0] for q in logs.queries} == {"ws-1"} and "ExecutionName_s ==" in logs.queries[0][1]


def test_the_pool_machine_runs_the_agent_as_one_execution():
    from duckstring.catchment.pool_launcher import ContainerAppsPoolMachine

    client = _Client()
    la = _launcher(client)
    m = ContainerAppsPoolMachine("heavy", {"cpu": 4096, "memory": 8192}, la)
    m.start()
    container = _calls(client, "start")[0][3]["containers"][0]
    assert container["args"][:3] == ["duckstring.duck.pool_agent", "--pool", "heavy"]
    assert container["resources"] == {"cpu": 4.0, "memory": "8Gi"} and m.is_up()
    m._probed_at = 0
    assert m.check()
    client.status = "Stopped"
    m._probed_at = 0
    assert not m.check() and "disappeared" in m.error()
    m.start()
    m.stop()
    assert len(_calls(client, "stop")) == 1 and not m.is_up()


def test_resource_ids_split_by_segment_name():
    assert _resource_part(ENV, "subscriptions") == SUB
    assert _resource_part(ENV.replace("resourceGroups", "resourcegroups"), "resourceGroups") == "lake-rg"
    assert _resource_part(ENV, "managedEnvironments") == "ducks"


# ─── the provider ────────────────────────────────────────────────────────────────

@pytest.fixture
def no_ambient_azure(monkeypatch):
    for k in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_CLIENT_SECRET", "AZURE_FEDERATED_TOKEN_FILE",
              "AZURE_CLIENT_CERTIFICATE_PATH", "IDENTITY_ENDPOINT", "MSI_ENDPOINT", "DUCKSTRING_CONTAINERAPPS_IMAGE",
              "DUCKSTRING_CONTAINERAPPS_ENVIRONMENT", "DUCKSTRING_CONTAINERAPPS_IDENTITY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(azure, "_cli_signed_in", lambda: False)
    monkeypatch.setattr(azure, "_managed_identity_available", lambda: False)


class _Secrets:
    def __init__(self, values):
        self.values = values

    def names(self):
        return [{"name": n, "set_at": "t"} for n in self.values]

    def get(self, name):
        return self.values.get(name)


ROOT = "abfss://lake@acct.dfs.core.windows.net/duckstring"


def test_azure_credentials_come_from_a_principal_an_identity_or_secrets(no_ambient_azure, monkeypatch):
    p = providers.get("azure")
    assert not p.credentials_present(None)
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://localhost:42356/msi/token")
    assert p.credentials_present(None)
    monkeypatch.delenv("IDENTITY_ENDPOINT")
    principal = {"AZURE_CLIENT_ID": "c", "AZURE_TENANT_ID": "t", "AZURE_CLIENT_SECRET": "s"}
    assert p.credentials_present(_Secrets(principal))
    assert not p.credentials_present(_Secrets({"AZURE_CLIENT_ID": "c"}))
    for k, v in principal.items():
        monkeypatch.setenv(k, v)
    assert p.credentials_present(None)


def test_azure_secrets_load_into_the_environment_without_overriding_it(no_ambient_azure, monkeypatch):
    monkeypatch.setenv("AZURE_TENANT_ID", "real")
    providers.get("azure").load_env(_Secrets({"AZURE_TENANT_ID": "stored", "AZURE_CLIENT_ID": "c", "PGPASS": "x"}))
    import os

    assert os.environ["AZURE_TENANT_ID"] == "real" and os.environ["AZURE_CLIENT_ID"] == "c"
    assert "PGPASS" not in os.environ
    monkeypatch.delenv("AZURE_CLIENT_ID")


def test_an_azure_root_with_credentials_enables_container_apps(no_ambient_azure, monkeypatch):
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://localhost:42356/msi/token")
    st = cloud.cloud_status(ROOT, None)
    assert st["provider"] == "azure" and st["cloud_enabled"] is True
    assert st["backends"] == {"containerapps": False}  # no environment or identity configured yet
    monkeypatch.setenv("DUCKSTRING_CONTAINERAPPS_ENVIRONMENT", ENV)
    monkeypatch.setenv("DUCKSTRING_CONTAINERAPPS_IDENTITY", IDN)
    assert cloud.cloud_status(ROOT, None)["backends"] == {"containerapps": True}
    assert cloud.cloud_status(ROOT.replace("abfss", "az"), _Secrets({}))["fargate_enabled"] is False


def test_container_apps_needs_an_environment_and_an_identity(no_ambient_azure):
    assert cloud_deploy.missing_fields("containerapps", None) == ["environment", "identity"]
    assert cloud_deploy.missing_fields("containerapps", {"environment": ENV, "identity": IDN}) == []
    assert cloud_deploy.clean("containerapps", {"environment": ENV, "ami": "x"}) == {"environment": ENV}


def test_the_backend_defaults_to_the_releases_azure_image(no_ambient_azure, tmp_path):
    from duckstring import __version__

    remotes = providers.get("azure").build_backends(tmp_path, "http://x:1", token="t", data_root=ROOT,
                                                    dialback=RemoteDialback("http://x:1"))
    assert remotes["containerapps"].image == f"ghcr.io/duckstring-dev/duckstring:{__version__}-azure"


def test_azure_presets_are_consumption_sizes(tmp_path):
    from duckstring.catchment.db import connect, migrate
    from duckstring.catchment.driver import Driver
    from duckstring.catchment.launcher import NoopLauncher

    db = connect(tmp_path / "duck.db")
    migrate(db)
    d = Driver(db, tmp_path, "http://x", NoopLauncher(), data_root=ROOT)
    presets = {p["name"]: p for p in d.list_pools() if p.get("managed")}
    assert presets["XL"] == {**presets["XL"], "provider": "containerapps", "cpu": 4096, "memory": 8192}
    d.add_pool("light")
    assert d.get_pool("light")["provider"] == "containerapps"
    with pytest.raises(ValueError, match="containerapps, local on Azure"):
        d.add_pool("bad", provider="fargate")
