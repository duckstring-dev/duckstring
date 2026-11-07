"""The compute deployment config (plans/compute-config-ui.md): the settings a remote Duck needs to launch,
as a per-pool / per-dedicated JSON blob coalesced over the launcher's env defaults. Each backend's fields
come from its provider (:mod:`.providers`), keyed by the backend name a pool's ``provider`` column holds.

Kept dependency-light (no cloud SDKs, no DB) so the driver, the launchers, and the routes can all share the
field lists + validation.
"""

from __future__ import annotations

import os

from . import providers


def _spec(backend: str | None) -> providers.DeploySpec:
    spec = providers.deploy_spec((backend or "fargate").lower())
    return spec if spec is not None else providers.DeploySpec(fields=(), env={})


def fields(backend: str | None) -> tuple[str, ...]:
    return _spec(backend).fields


def env_defaults(backend: str | None) -> dict:
    """The deployment fields this Catchment's environment already supplies (empty values dropped) — so the
    UI can show them as inherited and validation can account for them."""
    return {field: os.environ[var] for field, var in _spec(backend).env.items() if os.environ.get(var)}


def effective(backend: str | None, config: dict | None) -> dict:
    """The launch-time deployment config: the UI blob's non-empty values over the env defaults."""
    merged = dict(env_defaults(backend))
    for k, v in (config or {}).items():
        if v not in (None, ""):
            merged[k] = v
    return merged


def missing_fields(backend: str | None, config: dict | None) -> list[str]:
    """The required deployment fields still absent from the *effective* config (UI ?? env). Empty = ready
    to launch. ``config`` is the UI blob (env defaults are added here)."""
    spec = _spec(backend)
    eff = effective(backend, config)
    missing = [f for f in spec.required if not eff.get(f)]
    for group in reversed(spec.one_of):
        if not any(eff.get(f) for f in group):
            missing.insert(0, group[0])  # e.g. Fargate's image (or task_definition)
    return missing


def clean(backend: str | None, config: dict | None) -> dict:
    """Keep only the known deployment fields for the backend, dropping empties: what we persist."""
    known = _spec(backend).fields
    return {k: v for k, v in (config or {}).items() if k in known and v not in (None, "")}
