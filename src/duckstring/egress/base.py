"""The egress-driver seam (see plans/egress.md).

Mirrors :class:`duckstring.dataplane.DataPlane`: a small, scheme-selected interface the Spout machinery
threads a relation (or, later, a `Delta`) through. The transform stays framework code — a user writes
none. ``get_egress(destination)`` resolves the driver by the destination URI's scheme.

v1 ships the **object store** driver (snapshot writes, ``supports_delta=False``). The incremental
object-store path and the transactional Postgres driver (``apply_delta`` — the changelog-as-CDC sink)
land next; this interface is the seam they slot into.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Protocol, runtime_checkable

from .destination import Destination, DestinationError, parse_destination

if TYPE_CHECKING:  # avoid importing duckdb at module load
    import duckdb


@dataclass(frozen=True)
class Capabilities:
    """What a destination can do — decides how the worker reads/writes.

    - ``supports_delta`` — can apply an incremental change-set (else the worker always ``write_full``s).
    - ``supports_delete`` — can remove rows (a merge Trickle's retractions).
    - ``transactional`` — applies atomically and owns its watermark in-destination (exactly-once);
      otherwise the Catchment owns the watermark and delivery is idempotent at-least-once.
    """

    supports_delta: bool
    supports_delete: bool
    transactional: bool


@runtime_checkable
class EgressDriver(Protocol):
    def capabilities(self) -> Capabilities: ...

    def ensure(self, *, table: str, schema: dict | None, pk: list[str] | None) -> None:
        """Create/verify the destination shape for ``table`` (idempotent). A no-op for schemaless
        destinations (object store); creates the table + PK for a transactional one."""
        ...

    def write_full(self, relation: "duckdb.DuckDBPyRelation", *, table: str,
                   pk: list[str] | None, f: datetime) -> None:
        """Snapshot/replace ``table`` at the destination with the relation's current rows."""
        ...

    def apply_delta(self, delta, *, table: str, pk: list[str] | None, f: datetime) -> None:
        """Apply an incremental change-set (upserts + deletes). Only when ``supports_delta``."""
        ...


_REGISTRY: dict[str, Callable[[Destination], EgressDriver]] = {}


def register(scheme: str, factory: Callable[[Destination], EgressDriver]) -> None:
    _REGISTRY[scheme] = factory


def get_egress(destination: str) -> EgressDriver:
    """Resolve the egress driver for a destination URI by its scheme. Raises :class:`DestinationError`
    for an unknown scheme, or for a known scheme whose driver is not implemented yet."""
    dest = parse_destination(destination)  # validates scheme ∈ known + credential-reference syntax
    factory = _REGISTRY.get(dest.scheme)
    if factory is None:
        raise DestinationError(
            f"egress driver for scheme {dest.scheme!r} is not implemented yet (built: "
            f"{', '.join(sorted(_REGISTRY)) or 'none'})"
        )
    return factory(dest)


# Register the bundled drivers. Imported here (not at package import) to keep the dependency one-way.
def _register_builtins() -> None:
    from .object_store import ObjectStoreEgressDriver

    for scheme in ObjectStoreEgressDriver.SCHEMES:
        register(scheme, ObjectStoreEgressDriver)


_register_builtins()
