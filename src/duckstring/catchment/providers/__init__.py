"""The cloud providers a Catchment can run remote Ducks on (plans/azure-gcp.md, phase 2).

A Catchment has one cloud, picked by its data root's scheme (``s3`` → AWS, ``az``/``abfs[s]`` → Azure, ``gs``
→ Google): remote Ducks run next to their data, and a pipeline that spans clouds is a Catchment per cloud
joined by Ducts. Each :class:`Provider` holds what is specific to its cloud: the credential gate, the live
credential check, the data-root probe, its Duck backends (the ``provider`` column of a pool names a backend,
not a cloud) with their deployment fields, the S/M/L/XL presets, and the instance-type listing.

Catchment Ducks (subprocesses on the Catchment's own machine) read any data root, so a laptop with a bucket
needs no provider at all. Azure and Google offer storage only until phases 3 and 4 give them backends.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

# The preset pool names every provider backs with its own sizes, so `duck = "M"` means "medium on this cloud".
PRESET_NAMES = ("S", "M", "L", "XL")
# A shared Pool machine on the Catchment's own box (dev/test): needs no cloud.
LOCAL_BACKEND = "local"


@dataclass(frozen=True)
class DeploySpec:
    """The deployment config a backend's launcher needs, as fields the UI collects (beyond the size and
    region on the pool row), the env var each defaults from, and which are required. ``one_of`` groups name
    fields of which at least one is required (reported missing under the group's first name)."""

    fields: tuple[str, ...]
    env: dict[str, str]
    required: tuple[str, ...] = ()
    one_of: tuple[tuple[str, ...], ...] = ()


@dataclass
class Provider:
    """One cloud. The defaults are a storage-only cloud: no backends, so no remote compute is offered."""

    name: str
    title: str
    storage_schemes: tuple[str, ...]
    secret_prefixes: tuple[str, ...] = ()
    backends: dict[str, DeploySpec] = field(default_factory=dict)  # backend name → its deployment fields
    default_backend: str | None = None
    presets: dict[str, dict] = field(default_factory=dict)  # preset name → {provider (a backend), cpu, memory}

    def credentials_present(self, secret_store=None) -> bool:
        """Whether the control plane has credentials to launch with (presence only: deterministic and
        network-free, so a transient outage never flips the gate)."""
        return False

    def load_env(self, secret_store=None) -> None:
        """Load this cloud's credential secrets into the environment, so its SDKs' default chains see them."""

    def apply_secret(self, name: str, value: str, secret_store) -> None:
        """A credential secret was just set: make it live now (an explicit set wins over an ambient value)."""
        os.environ[name] = value

    def credentials_changed(self) -> None:
        """A credential secret was just set into the environment: drop anything cached about the credentials."""

    def validate(self) -> str | None:
        """A live credential check: None when the control-plane credentials authenticate, else the error."""
        return f"remote compute on {self.title} isn't available yet"

    def verify(self, data_root: str | None) -> dict:
        """The Verify button: ``{ok, error?, ...identity}`` plus ``bucket_ok``/``bucket_error`` when a
        ``data_root`` on this cloud is given (a probe object written and deleted)."""
        return {"ok": False, "error": self.validate()}

    def instance_types(self, region: str | None) -> dict:
        """``{available, region?, types: [{name, vcpu, memory_gib, gpu}], error?}`` for the VM sizes on offer."""
        return {"available": False, "error": f"instance types on {self.title} aren't listed yet", "types": []}

    def relay(self, base_url: str | None):
        """A relay that makes a Catchment behind NAT reachable to this cloud's Ducks, or None (the Catchment
        then needs ``DUCKSTRING_CATCHMENT_PUBLIC_URL``)."""
        return None

    def build_backends(self, root, base_url: str | None, *, token: str, data_root: str | None, dialback) -> dict:
        """``{backend: launcher}`` for this cloud's remote Ducks, sharing ``dialback``."""
        return {}

    def pool_machine(self, backend: str, pool: str, spec: dict, remote):
        """The machine a named Pool on ``backend`` runs its agent on, over that backend's launcher."""
        raise ValueError(f"{self.title} has no {backend!r} pool machine")

    @property
    def compute(self) -> bool:
        return bool(self.backends)


def _registry() -> dict[str, Provider]:
    from .aws import AWS
    from .azure import AZURE
    from .gcp import GCP

    return {p.name: p for p in (AWS, AZURE, GCP)}


_PROVIDERS: dict[str, Provider] | None = None


def providers() -> dict[str, Provider]:
    """Every provider by name (``aws``, ``azure``, ``gcp``)."""
    global _PROVIDERS
    if _PROVIDERS is None:
        _PROVIDERS = _registry()
    return _PROVIDERS


def get(name: str) -> Provider:
    return providers()[name]


def for_data_root(data_root: str | None) -> Provider | None:
    """The Catchment's cloud: the provider whose storage scheme the data root uses. None for a local root
    (or an object store no provider claims, such as a Databricks Volume)."""
    if not data_root or "://" not in data_root:
        return None
    scheme = urlsplit(data_root).scheme.lower()
    for p in providers().values():
        if scheme in p.storage_schemes:
            return p
    return None


def for_backend(backend: str | None) -> Provider | None:
    """The provider a Duck backend belongs to (``fargate`` → AWS); None for ``local`` or an unknown name."""
    for p in providers().values():
        if backend in p.backends:
            return p
    return None


def deploy_spec(backend: str | None) -> DeploySpec | None:
    p = for_backend(backend)
    return p.backends[backend] if p is not None else None


def all_backends() -> list[str]:
    """Every remote backend name across the providers, in registry order."""
    return [b for p in providers().values() for b in p.backends]


def secret_matches(name: str) -> Provider | None:
    """The provider whose credential secrets a secret named ``name`` is (setting one refreshes the gate)."""
    for p in providers().values():
        if any(name.startswith(prefix) for prefix in p.secret_prefixes):
            return p
    return None
