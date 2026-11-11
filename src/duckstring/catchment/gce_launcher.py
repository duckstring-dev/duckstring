"""The Compute Engine Duck backend (plans/azure-gcp.md, phase 4): the EC2 analogue for GPUs and machines beyond
Cloud Run's 8 vCPU / 32 GiB. One VM per Duck spawn, with the pool's ``instance_type`` as the machine type.

The VM boots from a startup script (``vm_boot.boot_script``) that installs Duckstring with uv and runs the Duck,
which fetches its source artifact over the duck channel. It runs as the operator's service account (with
``roles/storage.objectAdmin`` on the bucket), reached through the metadata server, on the zone's network (the
project's ``default`` unless a subnetwork is set) with an ephemeral external IP for outbound unless
``assign_public_ip`` is ``false`` (the default network has no Cloud NAT). The boot disk is deleted with the VM.

A Duck's clean exit powers the OS off; Compute Engine then stops billing the machine but keeps billing its disk,
so the Catchment deletes it, as on Azure: ``release`` watches the VM until it stops, ``terminate`` deletes it at
once, and the first launch in a zone sweeps this Catchment's stopped Duck VMs (labelled with the Catchment).
``diagnose`` reads the VM's status and the tail of its serial port.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading

from ..duck.id_token import audience_args
from ..keys import split_pond_key
from . import gcp_rest
from .vm_boot import REMOTE_ROOT, WATCH_POLL_S, boot_script, default_pip_spec, delete_when_stopped, vm_name

log = logging.getLogger("duckstring.gce")

_COMPUTE = "https://compute.googleapis.com/compute/v1"
_DEFAULT_IMAGE = "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64"
_DEFAULT_SIZE = "e2-standard-2"
_NOT_LIVE = {"STOPPING", "STOPPED", "TERMINATED", "SUSPENDING", "SUSPENDED", "gone"}
_NO_CONFIG = (
    "Compute Engine cannot launch: no zone or service account configured. A gce pool needs DUCKSTRING_GCE_ZONE and "
    "DUCKSTRING_GCE_SERVICE_ACCOUNT (an email with storage access to the data root), or the same fields in its "
    "deploy config. Without them, set the Pond's compute to the Catchment (local) target.")


class GceLauncher:
    manages_processes = True
    name = "gce"
    default_size = _DEFAULT_SIZE

    def __init__(self, root, base_url: str | None, token: str = "", data_root: str | None = None,
                 *, dialback=None, zone: str | None = None, project: str | None = None,
                 service_account: str | None = None, image: str | None = None, network: str | None = None,
                 subnetwork: str | None = None, assign_public_ip: str | None = None, pip_spec: str | None = None,
                 disk_gb: int | None = None, session=None, reap_poll_s: float = WATCH_POLL_S):
        from .dialback import RemoteDialback

        self.root = root
        self.base_url = base_url
        self.dialback = dialback or RemoteDialback(base_url)
        self.dialback.register(self._drain_pending)
        self.token = token
        self.data_root = data_root
        env = os.environ.get
        self.zone = zone or env("DUCKSTRING_GCE_ZONE")
        self.project = project or env("DUCKSTRING_GCE_PROJECT")
        self.service_account = service_account or env("DUCKSTRING_GCE_SERVICE_ACCOUNT")
        self.image = image or env("DUCKSTRING_GCE_IMAGE") or _DEFAULT_IMAGE
        self.network = network or env("DUCKSTRING_GCE_NETWORK") or "global/networks/default"
        self.subnetwork = subnetwork or env("DUCKSTRING_GCE_SUBNETWORK")
        self.assign_public_ip = assign_public_ip or env("DUCKSTRING_GCE_ASSIGN_PUBLIC_IP") or "true"
        self.pip_spec = pip_spec or env("DUCKSTRING_GCE_PIP_SPEC") or default_pip_spec("gcp")
        self.disk_gb = disk_gb or int(env("DUCKSTRING_GCE_DISK_GB") or 20)
        # Labels this Catchment's Duck VMs, so a sweep never touches another Catchment's in a shared project.
        self.catchment_tag = hashlib.sha256(f"duckstring:{token}".encode()).hexdigest()[:16]
        self._injected = session is not None
        self._session = session
        self._session_project: str | None = None
        self._reap_poll_s = reap_poll_s
        self._vms: dict[str, tuple[str, str, str]] = {}  # pond key → (project, zone, vm name)
        self._pending: dict[str, tuple] = {}
        self._launch_errors: dict[str, str] = {}
        self._swept: set[tuple[str, str]] = set()

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

    # ─── one VM ─────────────────────────────────────────────────────────────────

    def _placement(self, dc: dict) -> tuple[str, str, str]:
        zone = dc.get("zone") or self.zone
        account = dc.get("service_account") or self.service_account
        if not (zone and account):
            raise ValueError(_NO_CONFIG)
        self._s()
        project = gcp_rest.project_id(dc.get("project") or self.project, self._session_project)
        if not project:
            raise ValueError("Compute Engine cannot launch: no project (set GOOGLE_CLOUD_PROJECT or DUCKSTRING_GCE_PROJECT)")
        return project, zone, account

    def _url(self, project: str, zone: str, name: str = "") -> str:
        return f"{_COMPUTE}/projects/{project}/zones/{zone}/instances" + (f"/{name}" if name else "")

    def _body(self, name: str, dc: dict, zone: str, size: str, args: list[str], account: str, role: str) -> dict:
        nic: dict = {"network": dc.get("network") or self.network}
        subnet = dc.get("subnetwork") or self.subnetwork
        if subnet:
            nic["subnetwork"] = subnet
        if str(dc.get("assign_public_ip") or self.assign_public_ip).lower() not in ("false", "0", "no", "disabled"):
            nic["accessConfigs"] = [{"type": "ONE_TO_ONE_NAT", "name": "External NAT"}]
        return {
            "name": name,
            "machineType": f"zones/{zone}/machineTypes/{size}",
            "labels": {"duckstring": role, "duckstring-catchment": self.catchment_tag},
            "disks": [{"boot": True, "autoDelete": True, "initializeParams": {
                "sourceImage": dc.get("image") or self.image, "diskSizeGb": str(dc.get("disk_gb") or self.disk_gb)}}],
            "networkInterfaces": [nic],
            "serviceAccounts": [{"email": account, "scopes": [gcp_rest.CLOUD_PLATFORM]}],
            "metadata": {"items": [{"key": "startup-script",
                                    "value": boot_script(args, dc.get("pip_spec") or self.pip_spec)}]},
        }

    def start_vm(self, name: str, dc: dict, size: str, args: list[str], role: str = "duck") -> tuple[str, str, str]:
        """Create a VM running ``python -m <args>`` (the insert is accepted at once; the VM boots in a minute)."""
        project, zone, account = self._placement(dc)
        self.sweep(project, zone)
        gcp_rest.call(self._s(), "POST", self._url(project, zone),
                      json=self._body(name, dc, zone, size, args, account, role))
        return project, zone, name

    def vm_state(self, address: tuple[str, str, str]) -> str:
        """The instance's status (``RUNNING``, ``TERMINATED``, …), ``"gone"`` when it doesn't exist."""
        try:
            return gcp_rest.call(self._s(), "GET", self._url(*address)).get("status") or "PROVISIONING"
        except gcp_rest.GcpError as exc:
            if exc.status == 404:
                return "gone"
            raise

    def vm_live(self, address: tuple[str, str, str]) -> bool:
        return self.vm_state(address) not in _NOT_LIVE

    def delete_vm(self, address: tuple[str, str, str]) -> None:
        try:
            gcp_rest.call(self._s(), "DELETE", self._url(*address))
            log.info("gce: deleting %s", address[2])
        except gcp_rest.GcpError as exc:
            if exc.status != 404:
                raise

    def sweep(self, project: str, zone: str) -> None:
        """Delete this Catchment's stopped Duck VMs in the zone, once per zone per process (a restarted
        Catchment's released VMs, which nothing else is watching)."""
        if (project, zone) in self._swept:
            return
        self._swept.add((project, zone))
        try:
            found = gcp_rest.call(self._s(), "GET", self._url(project, zone),
                                  params={"filter": f'labels.duckstring-catchment="{self.catchment_tag}"'})
            for vm in found.get("items") or []:
                if vm.get("status") in _NOT_LIVE:
                    self.delete_vm((project, zone, vm["name"]))
        except Exception:
            log.warning("gce: could not sweep stopped Duck VMs in %s", zone, exc_info=True)

    # ─── the launcher interface ─────────────────────────────────────────────────

    def set_base_url(self, url: str) -> None:
        self.base_url = url
        self.dialback.set_base_url(url)

    def _drain_pending(self) -> None:
        pending, self._pending = self._pending, {}
        for pond_key, (version, source_path, duck) in pending.items():
            self.ensure(pond_key, version, source_path, duck=duck)

    def is_running(self, pond_key: str) -> bool:
        return pond_key in self._pending or pond_key in self._vms

    def ensure(self, pond_key: str, version: str, source_path: str, duck: dict | None = None) -> None:
        if self.remote_base_url is None:
            self.dialback.request()
            self._pending[pond_key] = (version, source_path, duck)
            return
        if pond_key in self._vms:
            return
        name, major = split_pond_key(pond_key)
        pool = (duck or {}).get("pool") or {}
        dc = (duck or {}).get("deploy_config") or pool.get("deploy_config") or {}
        size = ((duck or {}).get("dedicated_instance_type") or pool.get("instance_type") or dc.get("instance_type")
                or _DEFAULT_SIZE)
        args = [
            "duckstring.duck", "--pond", name, "--major", str(major), "--version", version,
            "--catchment", self.remote_base_url, f"--token={self.token}",
            "--root", REMOTE_ROOT, "--source-path", source_path, f"--data-root={self.data_root or ''}",
            *audience_args(),
        ]
        try:
            address = self.start_vm(vm_name(pond_key), dc, size, args)
        except ValueError as exc:
            log.error("%s", exc)
            self._launch_errors[pond_key] = str(exc)
            return
        except Exception as exc:
            log.exception("gce: failed to create a Duck VM for %s", pond_key)
            self._launch_errors[pond_key] = f"gce: failed to create a Duck VM: {exc}"
            return
        self._launch_errors.pop(pond_key, None)
        self._vms[pond_key] = address
        log.info("gce: creating %s (%s) for %s", address[2], size, pond_key)

    def launch_error(self, pond_key: str) -> str | None:
        return self._launch_errors.get(pond_key)

    def diagnose(self, pond_key: str) -> str | None:
        """The VM's status and the tail of its serial port."""
        address = self._vms.get(pond_key)
        if address is None:
            return None
        parts = []
        try:
            vm = gcp_rest.call(self._s(), "GET", self._url(*address))
            parts.append(" ".join(p for p in (vm.get("status"), vm.get("statusMessage")) if p))
        except Exception as exc:
            parts.append(f"instance: {str(exc).splitlines()[0][:200]}")
        console = self.console_tail(address)
        if console:
            parts.append(f"console: {console}")
        return "gce: " + "; ".join(p for p in parts if p) if parts else None

    def console_tail(self, address: tuple[str, str, str], lines: int = 15) -> str | None:
        try:
            out = gcp_rest.call(self._s(), "GET", self._url(*address) + "/serialPort", params={"port": 1})
            tail = [ln for ln in (out.get("contents") or "").splitlines() if ln.strip()][-lines:]
            return " | ".join(tail) or None
        except Exception:
            log.debug("gce: serial port read failed for %s", address[2], exc_info=True)
            return None

    def release(self, pond_key: str) -> None:
        """The Duck was told to shut down: it finishes, exits and powers the OS off, and a watcher deletes the
        VM then (its disk is billed until it is). The next ensure() starts a fresh one."""
        self._pending.pop(pond_key, None)
        address = self._vms.pop(pond_key, None)
        if address is not None:
            threading.Thread(target=delete_when_stopped, args=(self, address), kwargs={"poll_s": self._reap_poll_s},
                             daemon=True, name=f"gce-reap-{address[2]}").start()

    def terminate(self, pond_key: str, wait: bool = False) -> None:
        self._pending.pop(pond_key, None)
        self._launch_errors.pop(pond_key, None)
        address = self._vms.pop(pond_key, None)
        if address is None:
            return
        try:
            self.delete_vm(address)
        except Exception:
            log.exception("gce: failed to delete %s for %s", address[2], pond_key)

    def shutdown_all(self) -> None:
        for pond_key in list(self._vms):
            self.terminate(pond_key)
        self._pending.clear()
