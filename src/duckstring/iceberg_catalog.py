"""A tiny **file-backed Iceberg catalog** — pyiceberg without SQLAlchemy.

pyiceberg's only embedded persistent catalog is ``SqlCatalog``, which pulls in SQLAlchemy purely to
store one pointer row per table (``namespace.table → current metadata.json``). Every actual Iceberg
operation — building table metadata, writing manifests, the FileIO — lives in the ``MetastoreCatalog``
base and the table layer. So this subclass keeps all of that and replaces *only* the pointer store
with a small JSON file (``catalog.json``), dropping the SQLAlchemy dependency.

It is deliberately minimal, matching how :mod:`duckstring.iceberg_plane` uses it: **one catalog per
``name@major`` line, a single namespace, one writer** (the Pond's Duck) with concurrent cross-Pond
*readers*. That single-writer invariant is why the commit path needs no optimistic-concurrency retry
loop (the ``SqlCatalog`` rowcount check guards against competing writers we don't have). Saves are
atomic (temp file + ``os.replace``) so a reader always sees a complete pointer map.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from pyiceberg.catalog import Catalog, CommitTableResponse, MetastoreCatalog
from pyiceberg.exceptions import (
    NamespaceAlreadyExistsError,
    NoSuchNamespaceError,
    NoSuchTableError,
    TableAlreadyExistsError,
)
from pyiceberg.io import load_file_io
from pyiceberg.partitioning import UNPARTITIONED_PARTITION_SPEC, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.serializers import FromInputFile
from pyiceberg.table import Table, TableProperties
from pyiceberg.table.locations import load_location_provider
from pyiceberg.table.metadata import new_table_metadata
from pyiceberg.table.sorting import UNSORTED_SORT_ORDER, SortOrder
from pyiceberg.table.update import TableRequirement, TableUpdate
from pyiceberg.typedef import EMPTY_DICT, Identifier, Properties

if TYPE_CHECKING:
    import pyarrow as pa


_CATALOG_FILE = "catalog.json"  # the pointer file name within the catalog location


class FileCatalog(MetastoreCatalog):
    """An Iceberg ``MetastoreCatalog`` whose pointer registry is a single JSON object. Construct with a
    ``warehouse`` URI (where tables live) and **either** a ``catalog_path`` (a local JSON file) **or** a
    ``pointer_storage`` (a :class:`~duckstring.storage.Storage` the ``catalog.json`` object lives in —
    object-store-capable). Routing the pointer through a ``Storage`` is what lets the whole catalog live in
    a bucket / Volume: a save is a single-object atomic PUT there (tmp+rename locally). Single-writer-per-
    line means no optimistic-concurrency check is needed."""

    def __init__(
        self, name: str, *, warehouse: str, catalog_path: str | os.PathLike | None = None,
        pointer_storage=None, **properties: str,
    ) -> None:
        super().__init__(name, warehouse=warehouse, **properties)
        self._storage = pointer_storage  # a Storage (object-store-capable) — wins over catalog_path
        self._path = Path(catalog_path) if catalog_path is not None else None
        self._state: dict = {"namespaces": {}, "tables": {}}
        raw = self._read_pointer()
        if raw is not None:
            self._state = json.loads(raw)
            self._state.setdefault("namespaces", {})
            self._state.setdefault("tables", {})

    # ─── JSON pointer store ─────────────────────────────────────────────────────

    def _read_pointer(self) -> str | None:
        if self._storage is not None:
            return self._storage.read_text(_CATALOG_FILE)
        if self._path is not None and self._path.exists():
            return self._path.read_text(encoding="utf-8")
        return None

    def _save(self) -> None:
        data = json.dumps(self._state)
        if self._storage is not None:
            self._storage.write_text(data, _CATALOG_FILE)  # atomic single-object PUT (object) / rename (local)
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, self._path)  # atomic for concurrent cross-Pond readers

    def _pointer(self, namespace: str, table: str) -> str | None:
        return self._state["tables"].get(namespace, {}).get(table)

    def _set_pointer(self, namespace: str, table: str, location: str) -> None:
        self._state["tables"].setdefault(namespace, {})[table] = location
        self._save()

    # ─── tables ─────────────────────────────────────────────────────────────────

    def create_table(
        self,
        identifier: str | Identifier,
        schema: Schema | "pa.Schema",
        location: str | None = None,
        partition_spec: PartitionSpec = UNPARTITIONED_PARTITION_SPEC,
        sort_order: SortOrder = UNSORTED_SORT_ORDER,
        properties: Properties = EMPTY_DICT,
    ) -> Table:
        schema = self._convert_schema_if_needed(  # type: ignore[assignment]
            schema, int(properties.get(TableProperties.FORMAT_VERSION, TableProperties.DEFAULT_FORMAT_VERSION))
        )
        namespace = Catalog.namespace_to_string(Catalog.namespace_from(identifier))
        table_name = Catalog.table_name_from(identifier)
        if not self.namespace_exists(namespace):
            raise NoSuchNamespaceError(f"Namespace does not exist: {namespace}")
        if self._pointer(namespace, table_name) is not None:
            raise TableAlreadyExistsError(f"Table {namespace}.{table_name} already exists")

        location = self._resolve_table_location(location, namespace, table_name)
        provider = load_location_provider(table_location=location, table_properties=properties)
        metadata_location = provider.new_table_metadata_file_location()
        metadata = new_table_metadata(
            location=location, schema=schema, partition_spec=partition_spec, sort_order=sort_order, properties=properties
        )
        io = load_file_io(properties=self.properties, location=metadata_location)
        self._write_metadata(metadata, io, metadata_location)
        self._set_pointer(namespace, table_name, metadata_location)
        return self.load_table(identifier)

    def commit_table(
        self, table: Table, requirements: tuple[TableRequirement, ...], updates: tuple[TableUpdate, ...]
    ) -> CommitTableResponse:
        identifier = table.name()
        namespace = Catalog.namespace_to_string(Catalog.namespace_from(identifier))
        table_name = Catalog.table_name_from(identifier)
        try:
            current_table: Table | None = self.load_table(identifier)
        except NoSuchTableError:
            current_table = None

        staged = self._update_and_stage_table(current_table, identifier, requirements, updates)
        if current_table and staged.metadata == current_table.metadata:
            return CommitTableResponse(metadata=current_table.metadata, metadata_location=current_table.metadata_location)
        self._write_metadata(metadata=staged.metadata, io=staged.io, metadata_path=staged.metadata_location)
        # Single writer per line → a plain pointer swap; no concurrent-writer rowcount check needed.
        self._set_pointer(namespace, table_name, staged.metadata_location)
        return CommitTableResponse(metadata=staged.metadata, metadata_location=staged.metadata_location)

    def load_table(self, identifier: str | Identifier) -> Table:
        namespace = Catalog.namespace_to_string(Catalog.namespace_from(identifier))
        table_name = Catalog.table_name_from(identifier)
        metadata_location = self._pointer(namespace, table_name)
        if metadata_location is None:
            raise NoSuchTableError(f"Table does not exist: {namespace}.{table_name}")
        io = load_file_io(properties=self.properties, location=metadata_location)
        metadata = FromInputFile.table_metadata(io.new_input(metadata_location))
        return Table(
            identifier=Catalog.identifier_to_tuple(namespace) + (table_name,),
            metadata=metadata,
            metadata_location=metadata_location,
            io=self._load_file_io(metadata.properties, metadata_location),
            catalog=self,
        )

    def drop_table(self, identifier: str | Identifier) -> None:
        namespace = Catalog.namespace_to_string(Catalog.namespace_from(identifier))
        table_name = Catalog.table_name_from(identifier)
        if self._state["tables"].get(namespace, {}).pop(table_name, None) is None:
            raise NoSuchTableError(f"Table does not exist: {namespace}.{table_name}")
        self._save()

    def list_tables(self, namespace: str | Identifier) -> list[Identifier]:
        ns = Catalog.namespace_to_string(namespace)
        if not self.namespace_exists(ns):
            raise NoSuchNamespaceError(f"Namespace does not exist: {ns}")
        return [Catalog.identifier_to_tuple(ns) + (t,) for t in self._state["tables"].get(ns, {})]

    # ─── namespaces ─────────────────────────────────────────────────────────────

    def create_namespace(self, namespace: str | Identifier, properties: Properties = EMPTY_DICT) -> None:
        ns = Catalog.namespace_to_string(namespace, NoSuchNamespaceError)
        if ns in self._state["namespaces"]:
            raise NamespaceAlreadyExistsError(f"Namespace {ns} already exists")
        self._state["namespaces"][ns] = dict(properties)
        self._save()

    def drop_namespace(self, namespace: str | Identifier) -> None:
        ns = Catalog.namespace_to_string(namespace)
        self._state["namespaces"].pop(ns, None)
        self._state["tables"].pop(ns, None)
        self._save()

    def list_namespaces(self, namespace: str | Identifier = ()) -> list[Identifier]:
        if namespace:  # single-level namespaces only
            return []
        return [Catalog.identifier_to_tuple(ns) for ns in self._state["namespaces"]]

    def load_namespace_properties(self, namespace: str | Identifier) -> Properties:
        ns = Catalog.namespace_to_string(namespace)
        if ns not in self._state["namespaces"]:
            raise NoSuchNamespaceError(f"Namespace does not exist: {ns}")
        return dict(self._state["namespaces"][ns])

    def update_namespace_properties(
        self, namespace: str | Identifier, removals: set[str] | None = None, updates: Properties = EMPTY_DICT
    ):
        ns = Catalog.namespace_to_string(namespace)
        if ns not in self._state["namespaces"]:
            raise NoSuchNamespaceError(f"Namespace does not exist: {ns}")
        props = self._state["namespaces"][ns]
        for key in removals or set():
            props.pop(key, None)
        props.update(updates)
        self._save()

    # ─── unused surface (views / rename / register) ─────────────────────────────

    def list_views(self, namespace: str | Identifier) -> list[Identifier]:
        return []

    def view_exists(self, identifier: str | Identifier) -> bool:
        return False

    def _unsupported(self, *_args, **_kwargs):
        raise NotImplementedError("FileCatalog supports only Duckstring's table operations")

    drop_view = _unsupported
    rename_table = _unsupported
    register_table = _unsupported
