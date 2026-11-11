"""The Cloud Run Duck backend (plans/azure-gcp.md, phase 4): Google's default remote launcher. Each Duck is one
**execution** of a Cloud Run job, started with an override carrying the Duck's args, the Google counterpart of a
Fargate task or a Container Apps execution.

Cloud Run can't override a single execution's CPU or memory, so the launcher creates one job per configuration
*and size* (image, service account, region, cpu, memory), named ``duckstring-duck-{hash}``. The job runs as the
operator's service account (with ``roles/storage.objectAdmin`` on the data root's bucket); a Duck reaches it
through the metadata server, which both google-auth (gcsfs) and the bearer token DuckDB gets read.

Cloud Run pulls from Artifact Registry (or Docker Hub), not ghcr, so there is no default image: set
``DUCKSTRING_CLOUDRUN_IMAGE``, or ``DUCKSTRING_CLOUDRUN_REGISTRY`` to an Artifact Registry remote repository that
proxies ghcr.io (the image is then ``{registry}/duckstring-dev/duckstring:{version}-gcp``).

``ensure`` = run the job (it returns once the execution exists), ``terminate`` = cancel it, ``is_running`` = the
execution record, ``diagnose`` = its counts and the tail of its Cloud Logging entries.
"""

from __future__ import annotations

import hashlib
import logging
import os

from ..duck.id_token import audience_args
from ..keys import split_pond_key
from . import gcp_rest
from .vm_boot import REMOTE_ROOT

log = logging.getLogger("duckstring.cloudrun")

_RUN = "https://run.googleapis.com/v2"
_LOGGING = "https://logging.googleapis.com/v2/entries:list"
_JOB_PREFIX = "duckstring-duck"
# How long one execution may run (Cloud Run's maximum, 7 days). A Duck exits on its own once idle (and after
# DUCKSTRING_DUCK_ORPHAN_MINUTES without contact), so this only stops a Duck kept busy by a recurring Refresh.
_TASK_TIMEOUT = "604800s"
_NO_CONFIG = (
    "Cloud Run cannot launch: no image, service account or region configured. A cloudrun pool needs "
    "DUCKSTRING_CLOUDRUN_REGION, DUCKSTRING_CLOUDRUN_SERVICE_ACCOUNT, and DUCKSTRING_CLOUDRUN_IMAGE (an Artifact "
    "Registry image) or DUCKSTRING_CLOUDRUN_REGISTRY (a remote repository proxying ghcr.io), or the same fields in "
    "its deploy config. Without them, set the Pond's compute to the Catchment (local) target.")


def _units(pool: dict, dc: dict, key: str, env: str, default: int) -> float:
    return float(pool.get(key) or dc.get(key) or os.environ.get(env) or default)


def default_image(registry: str | None) -> str | None:
    """The release's Google image through an Artifact Registry remote repository proxying ghcr.io."""
    if not registry:
        return None
    from .. import __version__

    return f"{registry.rstrip('/')}/duckstring-dev/duckstring:{__version__}-gcp"


class CloudRunLauncher:
    manages_processes = True
    name = "cloudrun"

    def __init__(self, root, base_url: str | None, token: str = "", data_root: str | None = None,
                 *, dialback=None, image: str | None = None, region: str | None = None, project: str | None = None,
                 service_account: str | None = None, registry: str | None = None, job: str | None = None,
                 session=None):
        from .dialback import RemoteDialback

        self.root = root
        self.base_url = base_url
        self.dialback = dialback or RemoteDialback(base_url)
        self.dialback.register(self._drain_pending)
        self.token = token
        self.data_root = data_root
        env = os.environ.get
        self.registry = registry or env("DUCKSTRING_CLOUDRUN_REGISTRY")
        self.image = image or env("DUCKSTRING_CLOUDRUN_IMAGE") or default_image(self.registry)
        self.region = region or env("DUCKSTRING_CLOUDRUN_REGION")
        self.project = project or env("DUCKSTRING_CLOUDRUN_PROJECT")
        self.service_account = service_account or env("DUCKSTRING_CLOUDRUN_SERVICE_ACCOUNT")
        self.job = job or env("DUCKSTRING_CLOUDRUN_JOB")
        self._injected = session is not None
        self._session = session
        self._session_project: str | None = None
        self._jobs: dict[tuple, str] = {}  # config → job name (created or found)
        self._executions: dict[str, tuple[str, str, str, str]] = {}  # pond key → (project, region, job, execution)
        # Labels this Catchment's jobs, so retiring old ones never touches another Catchment's in a shared project.
        self.catchment_tag = hashlib.sha256(f"duckstring:{token}".encode()).hexdigest()[:16]
        self._pending: dict[str, tuple] = {}
        self._launch_errors: dict[str, str] = {}

    @property
    def remote_base_url(self) -> str | None:
        return self.dialback.url

    def _s(self):
        if self._session is None:
            self._session, self._session_project = gcp_rest.default_session()
        return self._session

    def reset_client(self) -> None:
        if not self._injected:
            self._session = None

    # ─── the job ────────────────────────────────────────────────────────────────

    def size(self, pool: dict, dc: dict) -> tuple[str, str]:
        """``(cpu, memory)`` as Cloud Run spells them, from a pool's units (1024 = one vCPU, memory in MiB)."""
        cpu = _units(pool, dc, "cpu", "DUCKSTRING_CLOUDRUN_CPU", 1024) / 1024
        mib = _units(pool, dc, "memory", "DUCKSTRING_CLOUDRUN_MEMORY", 2048)
        return f"{cpu:g}", f"{mib:g}Mi"

    def _placement(self, dc: dict) -> tuple[str, str, str, str]:
        image = dc.get("image") or default_image(dc.get("registry")) or self.image
        region = dc.get("region") or self.region
        account = dc.get("service_account") or self.service_account
        if not (image and region and account):
            raise ValueError(_NO_CONFIG)
        self._s()
        project = gcp_rest.project_id(dc.get("project") or self.project, self._session_project)
        if not project:
            raise ValueError("Cloud Run cannot launch: no project (set GOOGLE_CLOUD_PROJECT or DUCKSTRING_CLOUDRUN_PROJECT)")
        return project, region, image, account

    def job_for(self, dc: dict, cpu: str, memory: str) -> tuple[str, str, str]:
        """``(project, region, job)`` for the config and size, creating the job on first use."""
        project, region, image, account = self._placement(dc)
        sig = (project, region, image, account, cpu, memory)
        if sig in self._jobs:
            return project, region, self._jobs[sig]
        name = dc.get("job") or self.job or f"{_JOB_PREFIX}-{hashlib.sha1(repr(sig).encode()).hexdigest()[:8]}"
        base = f"{_RUN}/projects/{project}/locations/{region}"
        try:
            gcp_rest.call(self._s(), "GET", f"{base}/jobs/{name}")
        except gcp_rest.GcpError as exc:
            if exc.status != 404:
                raise
            body = {
                "labels": {"duckstring": "duck", "duckstring-catchment": self.catchment_tag},
                "template": {"taskCount": 1, "template": {
                    "containers": [{"image": image, "resources": {"limits": {"cpu": cpu, "memory": memory}}}],
                    "serviceAccount": account, "maxRetries": 0, "timeout": _TASK_TIMEOUT,
                }},
            }
            op = gcp_rest.call(self._s(), "POST", f"{base}/jobs", params={"jobId": name}, json=body)
            gcp_rest.wait_operation(self._s(), f"{_RUN}/{op['name']}")
            log.info("cloudrun: job %s ready in %s", name, region)
            if not (dc.get("job") or self.job):
                self._retire_old_jobs(base, image, keep=name)
        self._jobs[sig] = name
        return project, region, name

    def _retire_old_jobs(self, base: str, image: str, keep: str) -> None:
        """Delete this Catchment's other auto-named jobs that run another image (an older Duckstring) and have no
        execution in flight: each new image makes a new job, and nothing else removes the old ones. Best effort."""
        try:
            jobs = gcp_rest.call(self._s(), "GET", f"{base}/jobs", params={"pageSize": 500}).get("jobs") or []
            for job in jobs:
                short = job["name"].rsplit("/", 1)[-1]
                if short == keep or not short.startswith(f"{_JOB_PREFIX}-"):
                    continue
                if (job.get("labels") or {}).get("duckstring-catchment") != self.catchment_tag:
                    continue
                if job["template"]["template"]["containers"][0]["image"] == image:
                    continue
                executions = gcp_rest.call(self._s(), "GET", f"{base}/jobs/{short}/executions").get("executions") or []
                if any(not e.get("completionTime") for e in executions):
                    continue
                gcp_rest.call(self._s(), "DELETE", f"{base}/jobs/{short}")
                log.info("cloudrun: deleting job %s", short)
        except Exception:
            log.warning("cloudrun: could not retire old Duck jobs", exc_info=True)

    def start_execution(self, dc: dict, args: list[str], cpu: str, memory: str) -> tuple[str, str, str, str]:
        """Run one execution of the job for this config and size with ``python -m <args>``; returns its address."""
        project, region, job = self.job_for(dc, cpu, memory)
        op = gcp_rest.call(self._s(), "POST", f"{_RUN}/projects/{project}/locations/{region}/jobs/{job}:run",
                           json={"overrides": {"containerOverrides": [{"args": args}]}})
        execution = (op.get("metadata") or {}).get("name", "").rsplit("/", 1)[-1]
        if not execution:
            raise gcp_rest.GcpError(500, f"run returned no execution: {op.get('name')}")
        return project, region, job, execution

    def _execution_url(self, address) -> str:
        project, region, job, execution = address
        return f"{_RUN}/projects/{project}/locations/{region}/jobs/{job}/executions/{execution}"

    def execution_status(self, address) -> str:
        ex = gcp_rest.call(self._s(), "GET", self._execution_url(address))
        if not ex.get("completionTime"):
            return "running"
        if ex.get("succeededCount"):
            return "succeeded"
        return "cancelled" if ex.get("cancelledCount") else "failed"

    def execution_live(self, address) -> bool:
        return self.execution_status(address) == "running"

    def stop_execution(self, address) -> None:
        gcp_rest.call(self._s(), "POST", self._execution_url(address) + ":cancel", json={})

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
            "--root", REMOTE_ROOT, "--source-path", source_path, f"--data-root={self.data_root or ''}",
            *audience_args(),
        ]
        try:
            address = self.start_execution(dc, args, *self.size(pool, dc))
        except ValueError as exc:
            log.error("%s", exc)
            self._launch_errors[pond_key] = str(exc)
            return
        except Exception as exc:
            log.exception("cloudrun: failed to start a Duck for %s", pond_key)
            self._launch_errors[pond_key] = f"cloudrun: failed to start a Duck: {exc}"
            return
        self._launch_errors.pop(pond_key, None)
        self._executions[pond_key] = address
        log.info("cloudrun: started %s for %s", address[3], pond_key)

    def launch_error(self, pond_key: str) -> str | None:
        return self._launch_errors.get(pond_key)

    def diagnose(self, pond_key: str) -> str | None:
        """The execution's state and the tail of its Cloud Logging entries, for a Duck that never dialled back."""
        address = self._executions.get(pond_key)
        if address is None:
            return None
        parts = []
        try:
            parts.append(f"execution {address[3]} {self.execution_status(address)}")
        except Exception:
            log.debug("cloudrun: status failed for %s", pond_key, exc_info=True)
        logs = self.log_tail(address)
        if logs:
            parts.append(f"logs: {logs}")
        return "cloudrun: " + "; ".join(parts) if parts else None

    def log_tail(self, address, lines: int = 12) -> str | None:
        from .fargate_launcher import _NOISE

        project, _region, job, execution = address
        body = {
            "resourceNames": [f"projects/{project}"],
            "filter": (f'resource.type="cloud_run_job" AND resource.labels.job_name="{job}" '
                       f'AND labels."run.googleapis.com/execution_name"="{execution}"'),
            "orderBy": "timestamp desc", "pageSize": max(lines * 10, 100),
        }
        try:
            entries = gcp_rest.call(self._s(), "POST", _LOGGING, json=body).get("entries") or []
        except Exception:
            log.debug("cloudrun: log query failed for %s", execution, exc_info=True)
            return None
        messages = [str(e.get("textPayload") or (e.get("jsonPayload") or {}).get("message") or "") for e in entries]
        messages = [m for m in messages[::-1] if m]
        useful = [m for m in messages if not _NOISE(m)]
        return " | ".join((useful or messages)[-lines:]) or None

    def release(self, pond_key: str) -> None:
        """The Duck was told to shut down and its execution ends when it exits: forget it without cancelling it
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
            log.info("cloudrun: cancelled %s (%s)", address[3], pond_key)
        except Exception:
            log.exception("cloudrun: failed to cancel %s for %s", address[3], pond_key)

    def shutdown_all(self) -> None:
        for pond_key in list(self._executions):
            self.terminate(pond_key)
        self._pending.clear()
