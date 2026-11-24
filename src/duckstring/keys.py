"""The runtime identity of a Pond is one **major line** of a name: ``{name}@{major}``.

Every layer that addresses an executing Pond — the engine state, the Driver's metadata/job queues,
the Duck protocol, the status payload — uses this key, so two majors of the same name run as fully
independent Ponds. Display surfaces split it back into name + major.
"""

from __future__ import annotations


def pond_key(name: str, major: int) -> str:
    return f"{name}@{major}"


def split_pond_key(key: str) -> tuple[str, int]:
    name, _, major = key.rpartition("@")
    return name, int(major)


def spec_major(version_spec: str) -> int:
    """The major line a ``[sources]`` version spec pins: ``"1.2.3"`` / ``"1.2.3?"`` → 1."""
    return int(version_spec.rstrip("?").split(".")[0])


def version_key(version: str) -> tuple[int, ...]:
    """A comparable key for a dotted numeric version ("1.2.3" → (1, 2, 3)). Shorter strings order below
    longer ones with the same prefix ("1.2" < "1.2.3"), which is the intended min_version semantic."""
    return tuple(int("".join(ch for ch in part if ch.isdigit()) or 0) for part in version.split("."))


# ─── App instances on other machines (plans/app-scaling.md) ──────────────────────
#
# A launcher keeps one record per key and passes the name before ``@`` as ``--pond``. An App instance it starts is
# keyed ``{name}~{instance}@{major}``, so each instance is its own record, and the agent splits the name back.

APP_AGENT = "duckstring.app_agent"


def instance_key(name: str, major: int, instance: int) -> str:
    return f"{name}~{instance}@{major}"


def split_instance(name: str) -> tuple[str, int | None]:
    """``"feedback~3"`` → ``("feedback", 3)``; a plain name → ``(name, None)``."""
    base, sep, iid = name.rpartition("~")
    return (base, int(iid)) if sep and iid.isdigit() else (name, None)


def worker_module(duck: dict | None) -> str:
    """The module a launcher runs: a Duck, or an App instance's agent when ``duck`` carries ``app_instance``."""
    return APP_AGENT if (duck or {}).get("app_instance") is not None else "duckstring.duck"
