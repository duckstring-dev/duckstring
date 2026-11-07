"""The Container Apps Duck backend (plans/azure-gcp.md, phase 3): Azure's default remote launcher. Each Duck
is one **execution** of a manual-trigger Container Apps job, started with a template override carrying the
Duck's args and size, the Azure counterpart of a Fargate task.

The operator provides a Container Apps environment and a user-assigned managed identity (with Storage Blob
Data Contributor on the data root's account); the launcher creates the job itself, once per configuration
(image, environment, identity, workload profile), as the Fargate launcher registers task definitions. The
subscription and resource group come from the environment's id, and the location and the Log Analytics
workspace from the environment. A Duck finds its identity through ``AZURE_CLIENT_ID`` (Azure's default
chain, in the Python SDKs and in DuckDB, needs it for a user-assigned identity), read from the job.

``ensure`` = start an execution (it returns once the execution exists; it doesn't wait for it to finish),
``terminate`` = stop it, ``is_running`` = the execution record, ``diagnose`` = its status plus its system
events and console tail from Log Analytics. Measured against a real environment (2026-11-07): the job accepts a
7-day ``replicaTimeout`` (a warm Duck outlives the 30-minute default), a start returns in about 3 s, and a
stop is immediate. The SDK is imported lazily; fake clients are injectable for tests.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import timedelta

from ..keys import split_pond_key

log = logging.getLogger("duckstring.containerapps")

_REMOTE_ROOT = "/var/lib/duckstring"
_CONTAINER = "duck"
_JOB_PREFIX = "duckstring-duck"
# How long one execution may run. A Duck exits on its own once idle (and after DUCKSTRING_DUCK_ORPHAN_MINUTES
# without contact), so this is only a backstop for a Duck kept busy by a recurring Refresh.
_REPLICA_TIMEOUT_S = 7 * 86400
_LIVE = {"running", "processing"}
_NO_CONFIG = (
    "Container Apps cannot launch: no image, environment or identity configured. A containerapps pool needs "
    "DUCKSTRING_CONTAINERAPPS_IMAGE, DUCKSTRING_CONTAINERAPPS_ENVIRONMENT (the managed environment's resource "
    "id) and DUCKSTRING_CONTAINERAPPS_IDENTITY (a user-assigned identity's resource id), or the same fields in "
    "its deploy config. Without them, set the Pond's compute to the Catchment (local) target.")


def _status(value) -> str:
    """An execution's status as a lower-case string (the SDK returns an enum)."""
    return str(getattr(value, "value", value) or "").lower()


def _resource_part(resource_id: str, key: str) -> str | None:
    """A segment of an ARM id (``subscriptions``, ``resourceGroups``, ``managedEnvironments``), by name."""
    parts = resource_id.strip("/").split("/")
    for i, p in enumerate(parts[:-1]):
        if p.lower() == key.lower():
            return parts[i + 1]
    return None


def _cpu(pool: dict, dc: dict) -> float:
    """vCPUs: a pool's cpu is in Fargate's units (1024 = one vCPU), so presets mean the same everywhere."""
    units = pool.get("cpu") or dc.get("cpu") or os.environ.get("DUCKSTRING_CONTAINERAPPS_CPU") or 1024
    return round(float(units) / 1024, 2)


def _memory(pool: dict, dc: dict) -> str:
    """Memory as Container Apps spells it (``2Gi``), from MiB."""
    mib = pool.get("memory") or dc.get("memory") or os.environ.get("DUCKSTRING_CONTAINERAPPS_MEMORY") or 2048
    return f"{float(mib) / 1024:g}Gi"


class ContainerAppsLauncher:
    manages_processes = True

    def __init__(self, root, base_url: str | None, token: str = "", data_root: str | None = None,
                 *, dialback=None, image: str | None = None, environment: str | None = None,
                 identity: str | None = None, workload_profile: str | None = None, job: str | None = None,
                 credential=None, client=None, logs_client=None):
        from .dialback import RemoteDialback

        self.root = root
        self.base_url = base_url
        self.dialback = dialback or RemoteDialback(base_url)
        self.dialback.register(self._drain_pending)
        self.token = token
        self.data_root = data_root
        self.image = image or os.environ.get("DUCKSTRING_CONTAINERAPPS_IMAGE")
        self.environment = environment or os.environ.get("DUCKSTRING_CONTAINERAPPS_ENVIRONMENT")
        self.identity = identity or os.environ.get("DUCKSTRING_CONTAINERAPPS_IDENTITY")
        self.workload_profile = (workload_profile or os.environ.get("DUCKSTRING_CONTAINERAPPS_WORKLOAD_PROFILE")
                                 or "Consumption")
        self.job = job or os.environ.get("DUCKSTRING_CONTAINERAPPS_JOB")
        self._credential = credential
        self._injected = client is not None  # a test's fake client: kept across reset_client
        self._clients: dict[str, object] = {"_injected": client} if client is not None else {}
        self._logs_client = logs_client
        self._environments: dict[str, object] = {}  # environment id → its resource (location, logs workspace)
        self._workspaces: dict[tuple[str, str], str] = {}  # (resource group, job) → Log Analytics workspace id
        self._jobs: dict[tuple, tuple[str, str, str | None]] = {}  # config → (resource group, job, client id)
        self._executions: dict[str, tuple[str, str, str, str]] = {}  # pond key → (sub, rg, job, execution)
        self._pending: dict[str, tuple] = {}
        self._launch_errors: dict[str, str] = {}

    @property
    def remote_base_url(self) -> str | None:
        return self.dialback.url

    # ─── clients ────────────────────────────────────────────────────────────────

    def _cred(self):
        if self._credential is None:
            from azure.identity import DefaultAzureCredential

            self._credential = DefaultAzureCredential()
        return self._credential

    def _client(self, subscription: str):
        if self._injected:
            return self._clients["_injected"]
        if subscription not in self._clients:
            from azure.mgmt.appcontainers import ContainerAppsAPIClient

            self._clients[subscription] = ContainerAppsAPIClient(self._cred(), subscription)
        return self._clients[subscription]

    def _logs(self):
        if self._logs_client is None:
            from azure.monitor.query import LogsQueryClient

            self._logs_client = LogsQueryClient(self._cred())
        return self._logs_client

    def reset_client(self) -> None:
        """Drop the cached credential and clients so added/rotated credentials take effect on the next spawn."""
        if not self._injected:
            self._clients = {}
            self._credential = None
            self._logs_client = None

    # ─── the job ────────────────────────────────────────────────────────────────

    def _environment(self, env_id: str):
        if env_id not in self._environments:
            sub, rg = _resource_part(env_id, "subscriptions"), _resource_part(env_id, "resourceGroups")
            name = _resource_part(env_id, "managedEnvironments")
            self._environments[env_id] = self._client(sub).managed_environments.get(rg, name)
        return self._environments[env_id]

    def job_for(self, dc: dict) -> tuple[str, str, str, str | None]:
        """``(subscription, resource group, job name, identity client id)`` for the effective config, creating
        (or updating) the job on first use. Raises ``ValueError`` when the config is incomplete."""
        image = dc.get("image") or self.image
        env_id = dc.get("environment") or self.environment
        identity = dc.get("identity") or self.identity
        if not (image and env_id and identity):
            raise ValueError(_NO_CONFIG)
        profile = dc.get("workload_profile") or self.workload_profile
        sig = (image, env_id.lower(), identity.lower(), profile)
        sub, rg = _resource_part(env_id, "subscriptions"), _resource_part(env_id, "resourceGroups")
        if not (sub and rg):
            raise ValueError(f"DUCKSTRING_CONTAINERAPPS_ENVIRONMENT is not a managed environment's resource id: {env_id}")
        if sig in self._jobs:
            return (sub, *self._jobs[sig])
        name = dc.get("job") or self.job or f"{_JOB_PREFIX}-{hashlib.sha1(repr(sig).encode()).hexdigest()[:8]}"
        env = self._environment(env_id)
        configuration = {
            "triggerType": "Manual", "replicaTimeout": _REPLICA_TIMEOUT_S, "replicaRetryLimit": 0,
            "manualTriggerConfig": {"parallelism": 1, "replicaCompletionCount": 1},
        }
        server = image.split("/", 1)[0]
        if server.endswith(".azurecr.io"):
            configuration["registries"] = [{"server": server, "identity": identity}]  # pulled with AcrPull
        body = {
            "location": env.location,
            "identity": {"type": "UserAssigned", "userAssignedIdentities": {identity: {}}},
            "properties": {
                "environmentId": env_id, "workloadProfileName": profile, "configuration": configuration,
                "template": {"containers": [{"name": _CONTAINER, "image": image,
                                             "resources": {"cpu": 1.0, "memory": "2Gi"}}]},
            },
            "tags": {"duckstring": "duck"},
        }
        job = self._client(sub).jobs.begin_create_or_update(rg, name, body).result()
        client_id = None
        for key, value in ((getattr(job, "identity", None) and job.identity.user_assigned_identities) or {}).items():
            if key.lower() == identity.lower():
                client_id = getattr(value, "client_id", None)
        self._jobs[sig] = (rg, name, client_id)
        logs = getattr(getattr(env, "app_logs_configuration", None), "log_analytics_configuration", None)
        if getattr(logs, "customer_id", None):
            self._workspaces[(rg, name)] = logs.customer_id
        log.info("containerapps: job %s ready in %s", name, rg)
        return sub, rg, name, client_id

    def start_execution(self, dc: dict, args: list[str], cpu: float, memory: str) -> tuple[str, str, str, str]:
        """Start one execution of the config's job running ``python -m <args>``; returns its address."""
        sub, rg, job, client_id = self.job_for(dc)
        image = dc.get("image") or self.image
        env = [{"name": "AZURE_CLIENT_ID", "value": client_id}] if client_id else []
        template = {"containers": [{"name": _CONTAINER, "image": image, "args": args, "env": env,
                                    "resources": {"cpu": cpu, "memory": memory}}]}
        execution = self._client(sub).jobs.begin_start(rg, job, template=template).result()
        return sub, rg, job, execution.name

    def execution_status(self, address: tuple[str, str, str, str]) -> str:
        sub, rg, job, name = address
        return _status(self._client(sub).job_execution(rg, job, name).status)

    def stop_execution(self, address: tuple[str, str, str, str]) -> None:
        sub, rg, job, name = address
        self._client(sub).jobs.begin_stop_execution(rg, job, name)

    # ─── the launcher interface ─────────────────────────────────────────────────

    def set_base_url(self, url: str) -> None:
        self.base_url = url
        self.dialback.set_base_url(url)

    def _drain_pending(self) -> None:
        pending, self._pending = self._pending, {}
        for pond_key, (version, source_path, duck) in pending.items():
            self.ensure(pond_key, version, source_path, duck=duck)

    def is_running(self, pond_key: str) -> bool:
        return pond_key in self._pending or pond_key in self._executions

    def ensure(self, pond_key: str, version: str, source_path: str, duck: dict | None = None) -> None:
        if self.remote_base_url is None:
            self.dialback.request()
            self._pending[pond_key] = (version, source_path, duck)
            return
        if pond_key in self._executions:
            return
        name, major = split_pond_key(pond_key)
        pool = (duck or {}).get("pool") or {}
        dc = (duck or {}).get("deploy_config") or pool.get("deploy_config") or {}
        args = [
            "duckstring.duck", "--pond", name, "--major", str(major), "--version", version,
            "--catchment", self.remote_base_url, f"--token={self.token}",
            "--root", _REMOTE_ROOT, "--source-path", source_path, f"--data-root={self.data_root or ''}",
        ]
        try:
            address = self.start_execution(dc, args, _cpu(pool, dc), _memory(pool, dc))
        except ValueError as exc:
            log.error("%s", exc)
            self._launch_errors[pond_key] = str(exc)
            return
        except Exception as exc:
            log.exception("containerapps: failed to start a Duck for %s", pond_key)
            self._launch_errors[pond_key] = f"containerapps: failed to start a Duck: {exc}"
            return
        self._launch_errors.pop(pond_key, None)
        self._executions[pond_key] = address
        log.info("containerapps: started %s for %s", address[3], pond_key)

    def launch_error(self, pond_key: str) -> str | None:
        return self._launch_errors.get(pond_key)

    def diagnose(self, pond_key: str) -> str | None:
        """The execution's status and the tail of its console log, for a Duck that never dialled back."""
        address = self._executions.get(pond_key)
        if address is None:
            return None
        parts = []
        try:
            parts.append(f"execution {address[3]} {self.execution_status(address)}")
        except Exception:
            log.debug("containerapps: status failed for %s", pond_key, exc_info=True)
        logs = self.log_tail(address)
        if logs:
            parts.append(f"logs: {logs}")
        return "containerapps: " + "; ".join(parts) if parts else None

    def _query(self, workspace: str, query: str) -> list:
        result = self._logs().query_workspace(workspace, query, timespan=timedelta(days=1))
        return [list(row) for table in result.tables for row in table.rows]

    def log_tail(self, address: tuple[str, str, str, str], lines: int = 12) -> str | None:
        """What Log Analytics holds for an execution: its system events (image pull, exit code, the reason a
        pod stopped), then the tail of its console log. The system log is the one that names why a Duck never
        dialled back; both arrive minutes late, so this is often empty for a Duck that just failed."""
        workspace = self._workspaces.get((address[1], address[2]))
        if not workspace:
            return None
        from .fargate_launcher import _NOISE

        job, execution = address[2].replace("'", ""), address[3].replace("'", "")
        parts = []
        try:
            events = self._query(workspace, (
                "ContainerAppSystemLogs_CL | where JobName_s == '%s' and ExecutionName_s == '%s' "
                "| project TimeGenerated, Reason_s, Log_s | order by TimeGenerated desc | take 6" % (job, execution)))
            parts += [f"{row[1]}: {row[2]}" for row in events[::-1]]
        except Exception:
            log.debug("containerapps: system log query failed for %s", address, exc_info=True)
        try:
            rows = self._query(workspace, (
                "ContainerAppConsoleLogs_CL | where ContainerJobName_s == '%s' and ContainerGroupName_s "
                "startswith '%s' | project TimeGenerated, Log_s | order by TimeGenerated desc | take %d"
                % (job, execution, max(lines * 10, 100))))
            messages = [str(row[1]) for row in rows][::-1]
            useful = [m for m in messages if not _NOISE(m)]
            parts += (useful or messages)[-lines:]
        except Exception:
            log.debug("containerapps: console log query failed for %s", address, exc_info=True)
        return " | ".join(parts) or None

    def release(self, pond_key: str) -> None:
        """The Duck was told to shut down and its execution ends when it exits: forget it without stopping it
        (it may still be persisting), so the next ensure() starts a fresh one."""
        self._pending.pop(pond_key, None)
        self._executions.pop(pond_key, None)

    def terminate(self, pond_key: str, wait: bool = False) -> None:
        self._pending.pop(pond_key, None)
        self._launch_errors.pop(pond_key, None)
        address = self._executions.pop(pond_key, None)
        if address is None:
            return
        try:
            self.stop_execution(address)
            log.info("containerapps: stopped %s (%s)", address[3], pond_key)
        except Exception:
            log.exception("containerapps: failed to stop %s for %s", address[3], pond_key)

    def shutdown_all(self) -> None:
        for pond_key in list(self._executions):
            self.terminate(pond_key)
        self._pending.clear()
