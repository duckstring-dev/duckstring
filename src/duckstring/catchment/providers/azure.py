"""Azure: Container Apps job and VM Ducks, ``az://`` and ``abfs[s]://`` data roots, credentials from Azure's default
chain (a service principal's ``AZURE_*`` environment or secrets, workload or managed identity, the Azure CLI)."""

from __future__ import annotations

import base64
import functools
import json
import os
from pathlib import Path

from . import DeploySpec, Provider

SECRET_PREFIX = "AZURE_"
_MANAGEMENT_SCOPE = "https://management.azure.com/.default"

# The Container Apps deployment config. The subscription, resource group, location and Log Analytics workspace
# all follow from the environment; the image defaults to the release's own -azure tag.
CONTAINERAPPS = DeploySpec(
    fields=("image", "environment", "identity", "workload_profile", "job"),
    env={
        "image": "DUCKSTRING_CONTAINERAPPS_IMAGE", "environment": "DUCKSTRING_CONTAINERAPPS_ENVIRONMENT",
        "identity": "DUCKSTRING_CONTAINERAPPS_IDENTITY",
        "workload_profile": "DUCKSTRING_CONTAINERAPPS_WORKLOAD_PROFILE", "job": "DUCKSTRING_CONTAINERAPPS_JOB",
    },
    required=("environment", "identity"),
)

# The Azure VM deployment config (the EC2 analogue). The resource group defaults to the subnet's.
AZUREVM = DeploySpec(
    fields=("image", "subnet", "identity", "location", "resource_group", "assign_public_ip", "pip_spec", "disk_gb"),
    env={
        "image": "DUCKSTRING_AZUREVM_IMAGE", "subnet": "DUCKSTRING_AZUREVM_SUBNET",
        "identity": "DUCKSTRING_AZUREVM_IDENTITY", "location": "DUCKSTRING_AZUREVM_LOCATION",
        "resource_group": "DUCKSTRING_AZUREVM_RESOURCE_GROUP",
        "assign_public_ip": "DUCKSTRING_AZUREVM_ASSIGN_PUBLIC_IP", "pip_spec": "DUCKSTRING_AZUREVM_PIP_SPEC",
        "disk_gb": "DUCKSTRING_AZUREVM_DISK_GB",
    },
    required=("subnet", "identity", "location"),
)

_INSTANCE_TYPES: dict[str, list[dict]] = {}  # location → sorted VM sizes; process-lifetime cache

# The presets as Container Apps consumption sizes: the same vCPUs as AWS's, at the consumption profile's
# 2 GiB per vCPU (its largest is 4 vCPU / 8 GiB). Units as on every pool: cpu in 1024ths of a vCPU, memory MiB.
PRESETS = {
    "S": {"provider": "containerapps", "cpu": 512, "memory": 1024},
    "M": {"provider": "containerapps", "cpu": 1024, "memory": 2048},
    "L": {"provider": "containerapps", "cpu": 2048, "memory": 4096},
    "XL": {"provider": "containerapps", "cpu": 4096, "memory": 8192},
}


def default_image() -> str:
    """The published Azure Duck image for this Duckstring (a Duck must run its Catchment's version)."""
    from ... import __version__

    return f"ghcr.io/duckstring-dev/duckstring:{__version__}-azure"


def _cli_signed_in() -> bool:
    """Whether the Azure CLI holds a login (its profile lists a subscription), which the default chain uses."""
    path = Path(os.environ.get("AZURE_CONFIG_DIR") or Path.home() / ".azure") / "azureProfile.json"
    try:
        return bool(json.loads(path.read_text(encoding="utf-8-sig")).get("subscriptions"))
    except Exception:
        return False


@functools.lru_cache(maxsize=1)
def _managed_identity_available() -> bool:
    """A managed identity behind the instance metadata service (an Azure VM): the one source with no sign in
    the environment. Probed once per process, like AWS's credential chain."""
    try:
        from azure.identity import ManagedIdentityCredential

        ManagedIdentityCredential().get_token(_MANAGEMENT_SCOPE)
        return True
    except Exception:
        return False


def _claims(token: str) -> dict:
    """The unverified claims of an access token (for naming the identity on the Verify button)."""
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:
        return {}


def _error(exc: Exception) -> str:
    msg = (str(exc) or exc.__class__.__name__).splitlines()[0]
    return msg if len(msg) <= 300 else msg[:297] + "..."


class AzureProvider(Provider):
    def credentials_present(self, secret_store=None) -> bool:
        env = os.environ
        if env.get("AZURE_CLIENT_ID") and env.get("AZURE_TENANT_ID") and (
                env.get("AZURE_CLIENT_SECRET") or env.get("AZURE_FEDERATED_TOKEN_FILE")
                or env.get("AZURE_CLIENT_CERTIFICATE_PATH")):
            return True  # a service principal, or workload identity
        if env.get("IDENTITY_ENDPOINT") or env.get("MSI_ENDPOINT"):
            return True  # a managed identity in App Service, Functions or Container Apps
        if secret_store is not None:
            try:
                names = {n["name"] for n in secret_store.names()}
                if {"AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_CLIENT_SECRET"} <= names:
                    return True
            except Exception:
                pass
        return _cli_signed_in() or _managed_identity_available()

    def load_env(self, secret_store=None) -> None:
        """Load ``AZURE_*`` secrets into the environment, where Azure's default chain (and DuckDB's) reads
        them. Real environment variables win."""
        if secret_store is None:
            return
        try:
            names = [n["name"] for n in secret_store.names() if n["name"].startswith(SECRET_PREFIX)]
        except Exception:
            return
        for name in names:
            if name not in os.environ:
                value = secret_store.get(name)
                if value is not None:
                    os.environ[name] = value

    def credentials_changed(self) -> None:
        _managed_identity_available.cache_clear()

    def _token(self):
        from azure.identity import DefaultAzureCredential

        return DefaultAzureCredential().get_token(_MANAGEMENT_SCOPE)

    def validate(self) -> str | None:
        """A management-scope token, and the configured Container Apps environment readable with it."""
        try:
            token = self._token()
        except Exception as exc:
            return _error(exc)
        env_id = os.environ.get("DUCKSTRING_CONTAINERAPPS_ENVIRONMENT")
        if not env_id:
            return None
        try:
            from azure.identity import DefaultAzureCredential
            from azure.mgmt.appcontainers import ContainerAppsAPIClient

            from ..containerapps_launcher import _resource_part

            sub = _resource_part(env_id, "subscriptions")
            client = ContainerAppsAPIClient(DefaultAzureCredential(), sub)
            client.managed_environments.get(_resource_part(env_id, "resourceGroups"),
                                            _resource_part(env_id, "managedEnvironments"))
        except Exception as exc:
            return f"signed in as {_claims(token.token).get('appid') or 'unknown'}, but: {_error(exc)}"
        return None

    def verify(self, data_root: str | None) -> dict:
        """A management token names the identity (tenant and object id); an Azure data root gets a probe
        object written and deleted through the data plane's own storage."""
        try:
            token = self._token()
        except Exception as exc:
            return {"ok": False, "error": _error(exc)}
        claims = _claims(token.token)
        err = self.validate() if os.environ.get("DUCKSTRING_CONTAINERAPPS_ENVIRONMENT") else None
        result = {"ok": err is None, "account": claims.get("tid"),
                  "arn": claims.get("upn") or claims.get("appid") or claims.get("oid"), "region": None}
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
        """The VM sizes offered in a location (Compute resource SKUs, without the ones restricted for this
        subscription), with vCPU / memory / GPU, cached per location for the process lifetime."""
        region = (region or "").strip() or os.environ.get("DUCKSTRING_AZUREVM_LOCATION")
        if not region:
            return {"available": False, "error": "no location: pass ?region= or set DUCKSTRING_AZUREVM_LOCATION",
                    "types": []}
        region = region.replace(" ", "").lower()
        if region in _INSTANCE_TYPES:
            return {"available": True, "region": region, "types": _INSTANCE_TYPES[region]}
        subscription = os.environ.get("AZURE_SUBSCRIPTION_ID") or _subscription_from_config()
        if not subscription:
            return {"available": False, "region": region, "types": [],
                    "error": "no subscription: set AZURE_SUBSCRIPTION_ID or DUCKSTRING_AZUREVM_SUBNET"}
        try:
            from azure.identity import DefaultAzureCredential
            from azure.mgmt.compute import ComputeManagementClient

            client = ComputeManagementClient(DefaultAzureCredential(), subscription)
            types = _vm_sizes(client.resource_skus.list(filter=f"location eq '{region}'"))
        except Exception as exc:
            return {"available": False, "region": region, "error": _error(exc), "types": []}
        _INSTANCE_TYPES[region] = types
        return {"available": True, "region": region, "types": types}

    def build_backends(self, root, base_url: str | None, *, token: str, data_root: str | None, dialback) -> dict:
        from ..azurevm_launcher import AzureVmLauncher
        from ..containerapps_launcher import ContainerAppsLauncher

        image = os.environ.get("DUCKSTRING_CONTAINERAPPS_IMAGE") or default_image()
        kw = dict(token=token, data_root=data_root, dialback=dialback)
        return {"containerapps": ContainerAppsLauncher(root, base_url, image=image, **kw),
                "azurevm": AzureVmLauncher(root, base_url, **kw)}

    def pool_machine(self, backend: str, pool: str, spec: dict, remote):
        from ..pool_launcher import ExecutionPoolMachine, VmPoolMachine

        return (VmPoolMachine if backend == "azurevm" else ExecutionPoolMachine)(pool, spec, remote)


def _subscription_from_config() -> str | None:
    from ..containerapps_launcher import _resource_part

    for var in ("DUCKSTRING_AZUREVM_SUBNET", "DUCKSTRING_CONTAINERAPPS_ENVIRONMENT"):
        if os.environ.get(var):
            return _resource_part(os.environ[var], "subscriptions")
    return None


def _vm_sizes(skus) -> list[dict]:
    """``virtualMachines`` SKUs as ``{name, vcpu, memory_gib, gpu}``, sorted like AWS's instance types."""
    out = []
    for sku in skus:
        if getattr(sku, "resource_type", None) != "virtualMachines":
            continue
        # A Zone restriction only rules out some zones (most SKUs carry one); a Location one, the whole region.
        if any(str(getattr(r.type, "value", r.type)) == "Location" for r in (sku.restrictions or [])):
            continue
        caps = {c.name: c.value for c in (sku.capabilities or [])}
        try:
            out.append({"name": sku.name, "vcpu": int(caps.get("vCPUs", 0)),
                        "memory_gib": float(caps.get("MemoryGB", 0)), "gpu": int(caps.get("GPUs", 0) or 0)})
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda t: (t["vcpu"], t["memory_gib"], t["name"]))
    return out


AZURE = AzureProvider(
    name="azure", title="Azure", storage_schemes=("az", "abfs", "abfss"), secret_prefixes=(SECRET_PREFIX,),
    backends={"containerapps": CONTAINERAPPS, "azurevm": AZUREVM}, default_backend="containerapps", presets=PRESETS,
)
