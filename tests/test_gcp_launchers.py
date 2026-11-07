"""The Cloud Run and Compute Engine Duck backends and the Google provider (plans/azure-gcp.md, phase 4), against a
fake authorized session that answers like the REST APIs."""

from __future__ import annotations

import functools
import json
import os
import re
import time

import pytest

from duckstring.catchment import cloud, cloud_deploy, providers
from duckstring.catchment.cloudrun_launcher import CloudRunLauncher
from duckstring.catchment.dialback import RemoteDialback
from duckstring.catchment.gce_launcher import GceLauncher
from duckstring.catchment.providers import gcp

pytestmark = pytest.mark.timeout(10)

SA = "duck@proj.iam.gserviceaccount.com"


class _Resp:
    def __init__(self, status: int, body: dict | None = None):
        self.status_code, self._body = status, body
        self.content = b"" if body is None else json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self):
        return self._body


class _Session:
    """Records every call and answers from small in-memory state: jobs, executions and instances."""

    def __init__(self):
        self.calls, self.jobs, self.executions, self.instances = [], set(), {}, {}
        self.logs: list[dict] = []
        self.n = 0

    def request(self, method, url, timeout=None, params=None, json=None):
        self.calls.append((method, url, params, json))
        if m := re.search(r"/locations/[^/]+/jobs/([^/:]+)/executions/([^/:]+)(:cancel)?$", url):
            if m.group(3):
                self.executions[m.group(2)] = {"completionTime": "t", "cancelledCount": 1}
                return _Resp(200, {})
            return _Resp(200, self.executions[m.group(2)])
        if m := re.search(r"/locations/[^/]+/jobs/([^/:]+):run$", url):
            self.n += 1
            name = f"{m.group(1)}-exec{self.n}"
            self.executions[name] = {}
            return _Resp(200, {"name": "projects/proj/locations/r/operations/run", "metadata": {
                "name": f"projects/proj/locations/r/jobs/{m.group(1)}/executions/{name}"}})
        if m := re.search(r"/locations/[^/]+/jobs/([^/:]+)$", url):
            return _Resp(200, {}) if m.group(1) in self.jobs else _Resp(404, {"error": {"message": "not found"}})
        if url.endswith("/jobs") and method == "POST":
            self.jobs.add(params["jobId"])
            return _Resp(200, {"name": "projects/proj/locations/r/operations/create"})
        if "/operations/" in url:
            return _Resp(200, {"done": True})
        if url.endswith("entries:list"):
            return _Resp(200, {"entries": self.logs})
        if m := re.search(r"/zones/([^/]+)/instances(?:/([^/]+))?(/serialPort)?$", url):
            name = m.group(2)
            if m.group(3):
                return _Resp(200, {"contents": "boot\nTraceback: boom\n"})
            if name is None and method == "POST":
                self.instances[json["name"]] = {**json, "status": "PROVISIONING"}
                return _Resp(200, {"name": "op-insert"})
            if name is None:
                return _Resp(200, {"items": [v for v in self.instances.values()
                                             if params and v["labels"]["duckstring-catchment"] in params["filter"]]})
            if name not in self.instances:
                return _Resp(404, {"error": {"message": "not found"}})
            if method == "DELETE":
                self.instances.pop(name)
                return _Resp(200, {"name": "op-delete"})
            return _Resp(200, self.instances[name])
        return _Resp(500, {"error": {"message": f"unexpected {method} {url}"}})


def _calls(session, method, fragment):
    return [c for c in session.calls if c[0] == method and fragment in c[1]]


# ─── Cloud Run ───────────────────────────────────────────────────────────────────

def _run_launcher(session=None, **kw):
    kw.setdefault("image", "australia-southeast1-docker.pkg.dev/proj/ducks/duckstring:dev-gcp")
    kw.setdefault("region", "australia-southeast1")
    kw.setdefault("service_account", SA)
    return CloudRunLauncher("/tmp/x", "https://cat.example", token="tok", data_root="gs://bucket/d",
                            dialback=RemoteDialback("https://cat.example"), project="proj",
                            session=session or _Session(), **kw)


def test_a_duck_is_an_execution_of_a_job_made_once_per_config_and_size():
    s = _Session()
    la = _run_launcher(s)
    la.ensure("sales@1", "1.0.0", "ponds/sales/1.0.0", duck={"pool": {"cpu": 2048, "memory": 8192}})
    la.ensure("reports@1", "1.0.0", "ponds/reports/1.0.0", duck={"pool": {"cpu": 2048, "memory": 8192}})
    la.ensure("big@1", "1.0.0", "ponds/big/1.0.0", duck={"pool": {"cpu": 4096, "memory": 16384}})
    creates = _calls(s, "POST", "/locations/australia-southeast1/jobs")
    creates = [c for c in creates if c[1].endswith("/jobs")]
    assert len(creates) == 2  # one per size: Cloud Run can't override an execution's CPU or memory
    _, url, params, body = creates[0]
    assert "/projects/proj/" in url and params["jobId"].startswith("duckstring-duck-")
    task = body["template"]["template"]
    assert task["serviceAccount"] == SA and task["maxRetries"] == 0 and task["timeout"] == "604800s"
    assert task["containers"][0]["resources"] == {"limits": {"cpu": "2", "memory": "8192Mi"}}
    runs = [c for c in s.calls if c[1].endswith(":run")]
    args = runs[0][3]["overrides"]["containerOverrides"][0]["args"]
    assert args[:3] == ["duckstring.duck", "--pond", "sales"] and "--data-root=gs://bucket/d" in args
    assert la.is_running("sales@1") and la._executions["sales@1"][3].endswith("-exec1")


def test_an_existing_job_is_reused_not_recreated():
    s = _Session()
    la = _run_launcher(s, job="ducks")
    s.jobs.add("ducks")
    la.ensure("sales@1", "1.0.0", "p", duck={})
    assert not [c for c in s.calls if c[1].endswith("/jobs")] and [c for c in s.calls if c[1].endswith("ducks:run")]


def test_the_image_can_come_from_a_registry_proxying_ghcr():
    from duckstring import __version__

    la = _run_launcher(image=None, registry="australia-southeast1-docker.pkg.dev/proj/ghcr")
    assert la.image == f"australia-southeast1-docker.pkg.dev/proj/ghcr/duckstring-dev/duckstring:{__version__}-gcp"


def test_missing_config_fails_the_launch():
    s = _Session()
    la = _run_launcher(s, image="", service_account="")
    la.ensure("sales@1", "1.0.0", "p", duck={})
    assert "DUCKSTRING_CLOUDRUN_SERVICE_ACCOUNT" in la.launch_error("sales@1") and s.calls == []


def test_terminate_cancels_and_release_forgets():
    s = _Session()
    la = _run_launcher(s)
    la.ensure("sales@1", "1.0.0", "p", duck={})
    la.ensure("reports@1", "1.0.0", "p", duck={})
    la.release("reports@1")
    assert not la.is_running("reports@1") and not [c for c in s.calls if c[1].endswith(":cancel")]
    la.terminate("sales@1")
    assert [c for c in s.calls if c[1].endswith("-exec1:cancel")] and not la.is_running("sales@1")


def test_diagnose_reports_the_state_and_the_log_tail():
    s = _Session()
    la = _run_launcher(s)
    la.ensure("sales@1", "1.0.0", "p", duck={})
    exe = la._executions["sales@1"][3]
    s.executions[exe] = {"completionTime": "t", "failedCount": 1}
    s.logs = [{"textPayload": "Traceback: boom"}, {"textPayload": "GET /api/duck/sales/1/jobs 200 OK"}]
    got = la.diagnose("sales@1")
    assert f"execution {exe} failed" in got and "Traceback: boom" in got and "/jobs" not in got
    [q] = [c for c in s.calls if c[1].endswith("entries:list")]
    assert f'"run.googleapis.com/execution_name"="{exe}"' in q[3]["filter"]


def test_the_pool_machine_runs_the_agent_as_one_execution():
    from duckstring.catchment.pool_launcher import ExecutionPoolMachine

    s = _Session()
    la = _run_launcher(s)
    m = ExecutionPoolMachine("heavy", {"cpu": 8192, "memory": 32768}, la)
    m.start()
    [run] = [c for c in s.calls if c[1].endswith(":run")]
    assert run[3]["overrides"]["containerOverrides"][0]["args"][:3] == ["duckstring.duck.pool_agent", "--pool", "heavy"]
    m._probed_at = 0
    assert m.check()
    s.executions[m._execution[3]] = {"completionTime": "t", "succeededCount": 1}
    m._probed_at = 0
    assert not m.check() and "disappeared" in m.error()


# ─── Compute Engine ──────────────────────────────────────────────────────────────

def _gce(session=None, **kw):
    kw.setdefault("zone", "australia-southeast1-a")
    kw.setdefault("service_account", SA)
    return GceLauncher("/tmp/x", "https://cat.example", token="tok", data_root="gs://bucket/d",
                       dialback=RemoteDialback("https://cat.example"), project="proj", session=session or _Session(),
                       pip_spec="duckstring[gcp]==9.9", reap_poll_s=0.01, **kw)


def test_a_duck_is_a_vm_with_the_service_account_and_a_boot_script():
    s = _Session()
    la = _gce(s)
    la.ensure("sales@1", "1.0.0", "ponds/sales/1.0.0", duck={"pool": {"instance_type": "n2-highmem-8"}})
    [(_, url, _, body)] = [c for c in s.calls if c[0] == "POST" and c[1].endswith("/instances")]
    assert "/projects/proj/zones/australia-southeast1-a/instances" in url
    assert body["name"].startswith("duck-sales-1-")
    assert body["machineType"] == "zones/australia-southeast1-a/machineTypes/n2-highmem-8"
    assert body["serviceAccounts"] == [{"email": SA, "scopes": ["https://www.googleapis.com/auth/cloud-platform"]}]
    disk = body["disks"][0]
    assert disk["autoDelete"] is True and "ubuntu-2404-lts-amd64" in disk["initializeParams"]["sourceImage"]
    assert body["networkInterfaces"] == [{"network": "global/networks/default",
                                          "accessConfigs": [{"type": "ONE_TO_ONE_NAT", "name": "External NAT"}]}]
    assert body["labels"] == {"duckstring": "duck", "duckstring-catchment": la.catchment_tag}
    script = body["metadata"]["items"][0]["value"]
    assert "'duckstring[gcp]==9.9'" in script and "-m duckstring.duck --pond sales --major 1" in script
    assert la.is_running("sales@1")


def test_terminate_deletes_and_release_deletes_once_the_duck_has_exited():
    s = _Session()
    la = _gce(s)
    la.ensure("sales@1", "1.0.0", "p", duck={})
    la.ensure("reports@1", "1.0.0", "p", duck={})
    sales, reports = (la._vms["sales@1"][2], la._vms["reports@1"][2])
    la.terminate("sales@1")
    assert _calls(s, "DELETE", f"/instances/{sales}")
    s.instances[reports]["status"] = "RUNNING"
    la.release("reports@1")
    time.sleep(0.05)
    assert not _calls(s, "DELETE", f"/instances/{reports}")  # still running: its Duck is persisting
    s.instances[reports]["status"] = "TERMINATED"  # the Duck exited and powered the OS off
    for _ in range(100):
        if _calls(s, "DELETE", f"/instances/{reports}"):
            break
        time.sleep(0.01)
    assert _calls(s, "DELETE", f"/instances/{reports}") and reports not in s.instances


def test_a_launch_first_sweeps_this_catchments_stopped_vms():
    s = _Session()
    la = _gce(s)
    s.instances["duck-old-1"] = {"name": "duck-old-1", "status": "TERMINATED",
                                 "labels": {"duckstring-catchment": la.catchment_tag}}
    s.instances["duck-other-1"] = {"name": "duck-other-1", "status": "TERMINATED",
                                   "labels": {"duckstring-catchment": "someone-else"}}
    la.ensure("sales@1", "1.0.0", "p", duck={})
    assert "duck-old-1" not in s.instances and "duck-other-1" in s.instances


def test_no_public_ip_on_request_and_a_subnetwork():
    s = _Session()
    _gce(s, assign_public_ip="false", subnetwork="regions/r/subnetworks/ducks").ensure("sales@1", "1.0.0", "p", duck={})
    [(_, _, _, body)] = [c for c in s.calls if c[0] == "POST" and c[1].endswith("/instances")]
    assert body["networkInterfaces"] == [{"network": "global/networks/default", "subnetwork": "regions/r/subnetworks/ducks"}]


def test_gce_diagnose_reads_the_serial_port():
    s = _Session()
    la = _gce(s)
    la.ensure("sales@1", "1.0.0", "p", duck={})
    assert "PROVISIONING" in la.diagnose("sales@1") and "Traceback: boom" in la.diagnose("sales@1")


def test_missing_gce_config_fails_the_launch():
    la = _gce(zone="", service_account="")
    la.ensure("sales@1", "1.0.0", "p", duck={})
    assert "DUCKSTRING_GCE_ZONE" in la.launch_error("sales@1")


# ─── the provider ────────────────────────────────────────────────────────────────

@pytest.fixture
def no_ambient_google(monkeypatch):
    for k in ("GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT", "DUCKSTRING_CLOUDRUN_REGION",
              "DUCKSTRING_CLOUDRUN_SERVICE_ACCOUNT", "DUCKSTRING_CLOUDRUN_IMAGE", "DUCKSTRING_CLOUDRUN_REGISTRY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(gcp, "_default_credentials", functools.lru_cache(maxsize=1)(lambda: False))


class _Secrets:
    def __init__(self, root, values):
        self.path, self.values = root / "secrets.json", values

    def names(self):
        return [{"name": n, "set_at": "t"} for n in self.values]

    def get(self, name):
        return self.values.get(name)


def test_a_stored_key_becomes_a_private_credentials_file(no_ambient_google, tmp_path, monkeypatch):
    p = providers.get("gcp")
    store = _Secrets(tmp_path, {"GOOGLE_CREDENTIALS": '{"type": "service_account"}', "GOOGLE_CLOUD_PROJECT": "proj"})
    assert not p.credentials_present(None) and p.credentials_present(store)
    p.load_env(store)
    key = tmp_path / gcp.KEY_FILE
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == str(key) and os.environ["GOOGLE_CLOUD_PROJECT"] == "proj"
    assert key.read_text() == '{"type": "service_account"}' and oct(key.stat().st_mode & 0o777) == "0o600"
    p.apply_secret("GOOGLE_CREDENTIALS", '{"rotated": true}', store)  # a rotated key replaces our own file
    assert key.read_text() == '{"rotated": true}'
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/operator/key.json")
    p.apply_secret("GOOGLE_CREDENTIALS", '{"again": true}', store)  # but never the operator's
    assert key.read_text() == '{"rotated": true}'


def test_a_gcs_root_with_google_credentials_enables_cloud_run(no_ambient_google, monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/k.json")
    st = cloud.cloud_status("gs://bucket/p", None)
    assert st["provider"] == "gcp" and st["cloud_enabled"] is True
    assert st["backends"] == {"cloudrun": False, "gce": False}
    monkeypatch.setenv("DUCKSTRING_CLOUDRUN_REGION", "australia-southeast1")
    monkeypatch.setenv("DUCKSTRING_CLOUDRUN_SERVICE_ACCOUNT", SA)
    monkeypatch.setenv("DUCKSTRING_CLOUDRUN_REGISTRY", "australia-southeast1-docker.pkg.dev/proj/ghcr")
    assert cloud.cloud_status("gs://bucket/p", None)["backends"]["cloudrun"] is True


def test_cloud_run_needs_a_region_an_account_and_an_image_or_registry(no_ambient_google):
    assert cloud_deploy.missing_fields("cloudrun", None) == ["image", "region", "service_account"]
    assert cloud_deploy.missing_fields("cloudrun", {"registry": "r", "region": "x", "service_account": SA}) == []
    assert cloud_deploy.missing_fields("gce", {"zone": "z"}) == ["service_account"]


def test_google_presets_are_cloud_run_sizes(tmp_path):
    from duckstring.catchment.db import connect, migrate
    from duckstring.catchment.driver import Driver
    from duckstring.catchment.launcher import NoopLauncher

    db = connect(tmp_path / "duck.db")
    migrate(db)
    d = Driver(db, tmp_path, "http://x", NoopLauncher(), data_root="gs://bucket/p")
    presets = {p["name"]: p for p in d.list_pools() if p.get("managed")}
    assert presets["S"]["provider"] == "cloudrun" and (presets["S"]["cpu"], presets["S"]["memory"]) == (1024, 2048)
    with pytest.raises(ValueError, match="cloudrun, gce, local on Google Cloud"):
        d.add_pool("bad", provider="containerapps")
