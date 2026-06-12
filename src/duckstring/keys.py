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
