"""Google Cloud: Cloud Run job and Compute Engine Ducks, ``gs://`` data roots, credentials from application
default credentials (``GOOGLE_APPLICATION_CREDENTIALS``, the metadata server on Google compute, a ``gcloud``
login) or a service-account key stored as the ``GOOGLE_CREDENTIALS`` secret."""

from __future__ import annotations

import functools
import os
from pathlib import Path

from . import DeploySpec, Provider

SECRET_PREFIX = "GOOGLE_"
KEY_SECRET = "GOOGLE_CREDENTIALS"  # a service-account key's JSON, written to a 0600 file for the default chain
KEY_FILE = "google-credentials.json"  # beside the secret store; never in a download or a state backup

CLOUDRUN = DeploySpec(
    fields=("image", "registry", "region", "service_account", "project", "job"),
    env={
        "image": "DUCKSTRING_CLOUDRUN_IMAGE", "registry": "DUCKSTRING_CLOUDRUN_REGISTRY",
        "region": "DUCKSTRING_CLOUDRUN_REGION", "service_account": "DUCKSTRING_CLOUDRUN_SERVICE_ACCOUNT",
        "project": "DUCKSTRING_CLOUDRUN_PROJECT", "job": "DUCKSTRING_CLOUDRUN_JOB",
    },
    required=("region", "service_account"),
    one_of=(("image", "registry"),),
)
GCE = DeploySpec(
    fields=("zone", "service_account", "project", "image", "network", "subnetwork", "assign_public_ip", "pip_spec",
            "disk_gb"),
    env={
        "zone": "DUCKSTRING_GCE_ZONE", "service_account": "DUCKSTRING_GCE_SERVICE_ACCOUNT",
        "project": "DUCKSTRING_GCE_PROJECT", "image": "DUCKSTRING_GCE_IMAGE", "network": "DUCKSTRING_GCE_NETWORK",
        "subnetwork": "DUCKSTRING_GCE_SUBNETWORK", "assign_public_ip": "DUCKSTRING_GCE_ASSIGN_PUBLIC_IP",
        "pip_spec": "DUCKSTRING_GCE_PIP_SPEC", "disk_gb": "DUCKSTRING_GCE_DISK_GB",
    },
    required=("zone", "service_account"),
)

# The presets as Cloud Run job sizes: AWS's memory, with 1 vCPU for S (Cloud Run's smallest whole CPU).
PRESETS = {
    "S": {"provider": "cloudrun", "cpu": 1024, "memory": 2048},
    "M": {"provider": "cloudrun", "cpu": 1024, "memory": 4096},
    "L": {"provider": "cloudrun", "cpu": 2048, "memory": 8192},
    "XL": {"provider": "cloudrun", "cpu": 4096, "memory": 16384},
}

_INSTANCE_TYPES: dict[str, list[dict]] = {}  # zone → sorted machine types; process-lifetime cache


@functools.lru_cache(maxsize=1)
def _default_credentials() -> bool:
    """Whether application default credentials resolve (a key file, a ``gcloud`` login, the metadata server).
    Checked once per process, like AWS's credential chain."""
    try:
        import google.auth

        google.auth.default()
        return True
    except Exception:
        return False


def _error(exc: Exception) -> str:
    msg = (str(exc) or exc.__class__.__name__).splitlines()[0]
    return msg if len(msg) <= 300 else msg[:297] + "..."


class GcpProvider(Provider):
    def credentials_present(self, secret_store=None) -> bool:
        if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            return True
        if secret_store is not None:
            try:
                if any(n["name"] == KEY_SECRET for n in secret_store.names()):
                    return True
            except Exception:
                pass
        return _default_credentials()

    def load_env(self, secret_store=None) -> None:
        """Write a stored service-account key to a 0600 file and point ``GOOGLE_APPLICATION_CREDENTIALS`` at it;
        load other ``GOOGLE_*`` secrets (``GOOGLE_CLOUD_PROJECT``) as they are. Real environment variables win."""
        if secret_store is None:
            return
        try:
            names = [n["name"] for n in secret_store.names() if n["name"].startswith(SECRET_PREFIX)]
        except Exception:
            return
        for name in names:
            value = secret_store.get(name)
            if value is None:
                continue
            if name == KEY_SECRET:
                self._write_key(value, secret_store)
            elif name not in os.environ:
                os.environ[name] = value
        _default_credentials.cache_clear()

    def _write_key(self, value: str, secret_store) -> None:
        """The key to its 0600 file, pointed at by ``GOOGLE_APPLICATION_CREDENTIALS`` unless that already names
        another file (the operator's own wins)."""
        path = Path(secret_store.path).parent / KEY_FILE
        current = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if current and Path(current) != path:
            return
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(path)

    def apply_secret(self, name: str, value: str, secret_store) -> None:
        if name == KEY_SECRET:
            self._write_key(value, secret_store)
        else:
            os.environ[name] = value

    def credentials_changed(self) -> None:
        _default_credentials.cache_clear()

    def _session(self):
        from ..gcp_rest import default_session

        return default_session()

    def validate(self) -> str | None:
        """A token, and the configured Cloud Run region's jobs readable with it."""
        try:
            from google.auth.transport.requests import Request

            session, project = self._session()
            session.credentials.refresh(Request())
        except Exception as exc:
            return _error(exc)
        region = os.environ.get("DUCKSTRING_CLOUDRUN_REGION")
        project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("DUCKSTRING_CLOUDRUN_PROJECT") or project
        if not (region and project):
            return None
        try:
            from ..gcp_rest import call

            call(session, "GET", f"https://run.googleapis.com/v2/projects/{project}/locations/{region}/jobs",
                 params={"pageSize": 1})
        except Exception as exc:
            return _error(exc)
        return None

    def verify(self, data_root: str | None) -> dict:
        """The identity and project, and a probe object written to and deleted from a ``gs://`` data root."""
        try:
            session, project = self._session()
        except Exception as exc:
            return {"ok": False, "error": _error(exc)}
        err = self.validate()
        creds = session.credentials
        identity = getattr(creds, "service_account_email", None) or getattr(creds, "signer_email", None) or "user"
        result = {"ok": err is None, "account": os.environ.get("GOOGLE_CLOUD_PROJECT") or project, "arn": identity,
                  "region": os.environ.get("DUCKSTRING_CLOUDRUN_REGION")}
        if err:
            result["error"] = err
        root = (data_root or "").strip()
        if root and root.split("://", 1)[0].lower() in self.storage_schemes:
            try:
                from ...storage import get_storage

                store = get_storage(root)
                store.write_text("duckstring", ".duckstring-probe")
                store.remove(".duckstring-probe")
                result["bucket_ok"] = True
            except Exception as exc:
                result["bucket_ok"] = False
                result["bucket_error"] = _error(exc)
        return result

    def instance_types(self, region: str | None) -> dict:
        """Compute Engine machine types in a zone (a region means its ``-a`` zone), cached per zone."""
        zone = (region or "").strip() or os.environ.get("DUCKSTRING_GCE_ZONE")
        if not zone:
            return {"available": False, "error": "no zone: pass ?region= or set DUCKSTRING_GCE_ZONE", "types": []}
        if zone.count("-") == 1:
            zone += "-a"
        if zone in _INSTANCE_TYPES:
            return {"available": True, "region": zone, "types": _INSTANCE_TYPES[zone]}
        try:
            from ..gcp_rest import call

            session, project = self._session()
            project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("DUCKSTRING_GCE_PROJECT") or project
            types, token = [], None
            while True:
                page = call(session, "GET", f"https://compute.googleapis.com/compute/v1/projects/{project}/zones/"
                                            f"{zone}/machineTypes", params={"maxResults": 500, "pageToken": token})
                types += [{"name": t["name"], "vcpu": t.get("guestCpus"),
                           "memory_gib": round(t.get("memoryMb", 0) / 1024, 1),
                           "gpu": sum(a.get("guestAcceleratorCount", 0) for a in t.get("accelerators") or [])}
                          for t in page.get("items") or [] if not t.get("deprecated")]
                token = page.get("nextPageToken")
                if not token:
                    break
        except Exception as exc:
            return {"available": False, "region": zone, "error": _error(exc), "types": []}
        types.sort(key=lambda t: (t.get("vcpu") or 0, t.get("memory_gib") or 0, t["name"]))
        _INSTANCE_TYPES[zone] = types
        return {"available": True, "region": zone, "types": types}

    def build_backends(self, root, base_url: str | None, *, token: str, data_root: str | None, dialback) -> dict:
        from ..cloudrun_launcher import CloudRunLauncher
        from ..gce_launcher import GceLauncher

        kw = dict(token=token, data_root=data_root, dialback=dialback)
        return {"cloudrun": CloudRunLauncher(root, base_url, **kw), "gce": GceLauncher(root, base_url, **kw)}

    def pool_machine(self, backend: str, pool: str, spec: dict, remote):
        from ..pool_launcher import ExecutionPoolMachine, VmPoolMachine

        return (VmPoolMachine if backend == "gce" else ExecutionPoolMachine)(pool, spec, remote)


GCP = GcpProvider(
    name="gcp", title="Google Cloud", storage_schemes=("gs", "gcs"), secret_prefixes=(SECRET_PREFIX,),
    backends={"cloudrun": CLOUDRUN, "gce": GCE}, default_backend="cloudrun", presets=PRESETS,
)
