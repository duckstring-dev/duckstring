"""The Azure VM Duck backend (plans/azure-gcp.md, phase 3): the EC2 analogue for GPUs and machines beyond
Container Apps' sizes. One VM per Duck spawn, with the pool's ``instance_type`` as the VM size.

The VM boots through cloud-init custom data that installs Duckstring with uv and runs the Duck, which fetches
its source artifact over the duck channel (the same remote-boot path as EC2 and the containers). It gets a
user-assigned managed identity for the data root (with one user-assigned identity and no system identity,
the instance metadata service needs no client id), a NIC on the operator's subnet, a Standard public IP for
outbound unless ``assign_public_ip`` is ``false`` (new Azure subnets have no default outbound access), and an
SSH key nobody holds (Azure requires one; the Duck only dials out). NIC, IP and OS disk are created with
``deleteOption: Delete``, so deleting the VM removes them.

**Teardown is the Catchment's job.** On Azure a VM whose OS shuts down is stopped but still allocated, and
still billed, so a clean Duck exit (which powers the OS off) isn't enough: ``release`` watches the VM until it
stops and deletes it, ``terminate`` deletes it at once, and each launch first sweeps this Catchment's
stopped Duck VMs (tagged with the Catchment), which covers VMs released before a Catchment restart.
``diagnose`` reads the VM's provisioning status and the tail of its serial console (boot diagnostics), the
only window into a Duck that dies before it dials back.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import threading
import urllib.request

from ..keys import split_pond_key
from .containerapps_launcher import _resource_part
from .vm_boot import REMOTE_ROOT, WATCH_POLL_S, boot_script, default_pip_spec, delete_when_stopped, vm_name

log = logging.getLogger("duckstring.azurevm")

_DEFAULT_IMAGE = "Canonical:ubuntu-24_04-lts:server:latest"
_DEFAULT_SIZE = "Standard_D2s_v5"
_STOPPED = ("PowerState/stopped", "PowerState/deallocated")
_NO_CONFIG = (
    "Azure VM cannot launch: no subnet, identity or location configured. An azurevm pool needs "
    "DUCKSTRING_AZUREVM_SUBNET (a subnet's resource id), DUCKSTRING_AZUREVM_IDENTITY (a user-assigned identity's "
    "resource id) and DUCKSTRING_AZUREVM_LOCATION, or the same fields in its deploy config. Without them, set "
    "the Pond's compute to the Catchment (local) target.")


def _image_reference(image: str) -> dict:
    """A marketplace URN (``Publisher:Offer:Sku:Version``) or an image / gallery image version resource id."""
    if image.startswith("/"):
        return {"id": image}
    publisher, offer, sku, version = (image.split(":") + ["latest"])[:4]
    return {"publisher": publisher, "offer": offer, "sku": sku, "version": version or "latest"}


def _ssh_public_key() -> str:
    """A fresh RSA public key whose private half is discarded: Azure requires a credential on a Linux VM."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    return key.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                         serialization.PublicFormat.OpenSSH).decode()


class AzureVmLauncher:
    manages_processes = True
    name = "azurevm"
    default_size = _DEFAULT_SIZE

    def __init__(self, root, base_url: str | None, token: str = "", data_root: str | None = None,
                 *, dialback=None, image: str | None = None, subnet: str | None = None,
                 identity: str | None = None, location: str | None = None, resource_group: str | None = None,
                 assign_public_ip: str | None = None, pip_spec: str | None = None, disk_gb: int | None = None,
                 credential=None, client=None, reap_poll_s: float = WATCH_POLL_S):
        from .dialback import RemoteDialback

        self.root = root
        self.base_url = base_url
        self.dialback = dialback or RemoteDialback(base_url)
        self.dialback.register(self._drain_pending)
        self.token = token
        self.data_root = data_root
        env = os.environ.get
        self.image = image or env("DUCKSTRING_AZUREVM_IMAGE") or _DEFAULT_IMAGE
        self.subnet = subnet or env("DUCKSTRING_AZUREVM_SUBNET")
        self.identity = identity or env("DUCKSTRING_AZUREVM_IDENTITY")
        self.location = location or env("DUCKSTRING_AZUREVM_LOCATION")
        self.resource_group = resource_group or env("DUCKSTRING_AZUREVM_RESOURCE_GROUP")
        self.assign_public_ip = assign_public_ip or env("DUCKSTRING_AZUREVM_ASSIGN_PUBLIC_IP") or "true"
        self.pip_spec = pip_spec or env("DUCKSTRING_AZUREVM_PIP_SPEC") or default_pip_spec("azure")
        self.disk_gb = disk_gb or (int(env("DUCKSTRING_AZUREVM_DISK_GB")) if env("DUCKSTRING_AZUREVM_DISK_GB") else None)
        # Tags this Catchment's Duck VMs, so a sweep never touches another Catchment's in a shared group.
        self.catchment_tag = hashlib.sha256(f"duckstring:{token}".encode()).hexdigest()[:16]
        self._credential = credential
        self._injected = client is not None
        self._clients: dict[str, object] = {"_injected": client} if client is not None else {}
        self._reap_poll_s = reap_poll_s
        self._vms: dict[str, tuple[str, str, str]] = {}  # pond key → (subscription, resource group, vm name)
        self._pending: dict[str, tuple] = {}
        self._launch_errors: dict[str, str] = {}
        self._swept: set[tuple[str, str]] = set()

    @property
    def remote_base_url(self) -> str | None:
        return self.dialback.url

    def _cred(self):
        if self._credential is None:
            from azure.identity import DefaultAzureCredential

            self._credential = DefaultAzureCredential()
        return self._credential

    def _client(self, subscription: str):
        if self._injected:
            return self._clients["_injected"]
        if subscription not in self._clients:
            from azure.mgmt.compute import ComputeManagementClient

            self._clients[subscription] = ComputeManagementClient(self._cred(), subscription)
        return self._clients[subscription]

    def reset_client(self) -> None:
        if not self._injected:
            self._clients = {}
            self._credential = None

    # ─── one VM ─────────────────────────────────────────────────────────────────

    def _placement(self, dc: dict) -> tuple[str, str, str, str]:
        """``(subscription, resource group, subnet, identity)``: the group defaults to the subnet's."""
        subnet = dc.get("subnet") or self.subnet
        identity = dc.get("identity") or self.identity
        if not (subnet and identity and (dc.get("location") or self.location)):
            raise ValueError(_NO_CONFIG)
        sub = _resource_part(subnet, "subscriptions")
        rg = dc.get("resource_group") or self.resource_group or _resource_part(subnet, "resourceGroups")
        if not (sub and rg):
            raise ValueError(f"DUCKSTRING_AZUREVM_SUBNET is not a subnet's resource id: {subnet}")
        return sub, rg, subnet, identity

    def _body(self, name: str, dc: dict, size: str, args: list[str], subnet: str, identity: str, role: str) -> dict:
        ip_config: dict = {"subnet": {"id": subnet}}
        if str(dc.get("assign_public_ip") or self.assign_public_ip).lower() not in ("false", "0", "no", "disabled"):
            ip_config["publicIPAddressConfiguration"] = {
                "name": f"{name}-ip", "sku": {"name": "Standard"}, "properties": {"deleteOption": "Delete"}}
        os_disk: dict = {"createOption": "FromImage", "deleteOption": "Delete",
                         "managedDisk": {"storageAccountType": "StandardSSD_LRS"}}
        disk_gb = dc.get("disk_gb") or self.disk_gb
        if disk_gb:
            os_disk["diskSizeGB"] = int(disk_gb)
        script = boot_script(args, dc.get("pip_spec") or self.pip_spec)
        return {
            "location": dc.get("location") or self.location,
            "identity": {"type": "UserAssigned", "userAssignedIdentities": {identity: {}}},
            "tags": {"duckstring": role, "duckstring-catchment": self.catchment_tag},
            "properties": {
                "hardwareProfile": {"vmSize": size},
                "storageProfile": {"imageReference": _image_reference(dc.get("image") or self.image), "osDisk": os_disk},
                "osProfile": {
                    "computerName": name, "adminUsername": "duck",
                    "customData": base64.b64encode(script.encode()).decode(),
                    "linuxConfiguration": {"disablePasswordAuthentication": True, "ssh": {"publicKeys": [
                        {"path": "/home/duck/.ssh/authorized_keys", "keyData": _ssh_public_key()}]}},
                },
                "networkProfile": {"networkApiVersion": "2022-11-01", "networkInterfaceConfigurations": [{
                    "name": f"{name}-nic",
                    "properties": {"primary": True, "deleteOption": "Delete",
                                   "ipConfigurations": [{"name": "ipconfig", "properties": ip_config}]},
                }]},
                "diagnosticsProfile": {"bootDiagnostics": {"enabled": True}},  # managed storage: the serial log
            },
        }

    def start_vm(self, name: str, dc: dict, size: str, args: list[str], role: str = "duck") -> tuple[str, str, str]:
        """Create a VM running ``python -m <args>`` (the create is accepted at once; the VM boots in minutes)."""
        sub, rg, subnet, identity = self._placement(dc)
        self.sweep(sub, rg)
        self._client(sub).virtual_machines.begin_create_or_update(
            rg, name, self._body(name, dc, size, args, subnet, identity, role))
        return sub, rg, name

    def vm_state(self, address: tuple[str, str, str]) -> str | None:
        """The VM's ``PowerState/…`` code, ``"gone"`` when it doesn't exist, None while it has none yet."""
        sub, rg, name = address
        try:
            view = self._client(sub).virtual_machines.instance_view(rg, name)
        except Exception as exc:
            if "NotFound" in type(exc).__name__ or "ResourceNotFound" in str(exc) or "NotFound" in str(exc):
                return "gone"
            raise
        for status in getattr(view, "statuses", None) or []:
            if (status.code or "").startswith("PowerState/"):
                return status.code
        return None

    def vm_live(self, address: tuple[str, str, str]) -> bool:
        return self.vm_state(address) not in (*_STOPPED, "gone")

    def delete_vm(self, address: tuple[str, str, str]) -> None:
        sub, rg, name = address
        self._client(sub).virtual_machines.begin_delete(rg, name)
        log.info("azurevm: deleting %s", name)

    def sweep(self, sub: str, rg: str) -> None:
        """Delete this Catchment's stopped Duck VMs in the group, once per group per process (a restarted
        Catchment's released VMs, which nothing else is watching)."""
        if (sub, rg) in self._swept:
            return
        self._swept.add((sub, rg))
        try:
            for vm in self._client(sub).virtual_machines.list(rg):
                tags = getattr(vm, "tags", None) or {}
                if tags.get("duckstring-catchment") != self.catchment_tag:
                    continue
                if self.vm_state((sub, rg, vm.name)) in _STOPPED:
                    self.delete_vm((sub, rg, vm.name))
        except Exception:
            log.warning("azurevm: could not sweep stopped Duck VMs in %s", rg, exc_info=True)

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
        ]
        try:
            address = self.start_vm(vm_name(pond_key), dc, size, args)
        except ValueError as exc:
            log.error("%s", exc)
            self._launch_errors[pond_key] = str(exc)
            return
        except Exception as exc:
            log.exception("azurevm: failed to create a Duck VM for %s", pond_key)
            self._launch_errors[pond_key] = f"azurevm: failed to create a Duck VM: {exc}"
            return
        self._launch_errors.pop(pond_key, None)
        self._vms[pond_key] = address
        log.info("azurevm: creating %s (%s) for %s", address[2], size, pond_key)

    def launch_error(self, pond_key: str) -> str | None:
        return self._launch_errors.get(pond_key)

    def diagnose(self, pond_key: str) -> str | None:
        """The VM's provisioning and power status, and the tail of its serial console."""
        address = self._vms.get(pond_key)
        if address is None:
            return None
        sub, rg, name = address
        parts = []
        try:
            view = self._client(sub).virtual_machines.instance_view(rg, name)
            parts += [f"{s.code}{': ' + s.message if getattr(s, 'message', None) else ''}"
                      for s in (getattr(view, "statuses", None) or [])]
        except Exception as exc:
            parts.append(f"instance view: {str(exc).splitlines()[0][:200]}")
        console = self.console_tail(address)
        if console:
            parts.append(f"console: {console}")
        return "azurevm: " + "; ".join(parts) if parts else None

    def console_tail(self, address: tuple[str, str, str], lines: int = 15) -> str | None:
        sub, rg, name = address
        try:
            data = self._client(sub).virtual_machines.retrieve_boot_diagnostics_data(rg, name)
            uri = getattr(data, "serial_console_log_blob_uri", None)
            if not uri:
                return None
            with urllib.request.urlopen(uri, timeout=10) as resp:  # a short-lived SAS URL
                text = resp.read().decode("utf-8", "replace")
            tail = [ln for ln in text.splitlines() if ln.strip()][-lines:]
            return " | ".join(tail) or None
        except Exception:
            log.debug("azurevm: serial console read failed for %s", name, exc_info=True)
            return None

    def release(self, pond_key: str) -> None:
        """The Duck was told to shut down: it finishes, exits and powers the OS off, and a watcher deletes
        the VM then (a stopped Azure VM is still billed). The next ensure() starts a fresh one."""
        self._pending.pop(pond_key, None)
        address = self._vms.pop(pond_key, None)
        if address is not None:
            threading.Thread(target=delete_when_stopped, args=(self, address), kwargs={"poll_s": self._reap_poll_s},
                             daemon=True, name=f"azurevm-reap-{address[2]}").start()

    def terminate(self, pond_key: str, wait: bool = False) -> None:
        self._pending.pop(pond_key, None)
        self._launch_errors.pop(pond_key, None)
        address = self._vms.pop(pond_key, None)
        if address is None:
            return
        try:
            self.delete_vm(address)
        except Exception:
            log.exception("azurevm: failed to delete %s for %s", address[2], pond_key)

    def shutdown_all(self) -> None:
        for pond_key in list(self._vms):
            self.terminate(pond_key)
        self._pending.clear()
