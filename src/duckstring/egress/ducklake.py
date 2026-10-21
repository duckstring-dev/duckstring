"""DuckLake egress driver: a Pond's output delivered as DuckLake tables (see plans/duckdb-2.md, Part 2).

The destination is DuckDB's own attach string, with the Spout's options as a trailing query that this
driver removes before attaching (other query parameters, such as a Postgres catalog's ``sslmode``, stay)::

    ducklake:sqlite:/srv/lake/meta.sqlite?data_path=s3://bucket/lake/&schema=sales
    ducklake:postgres:dbname=lake host=pg.internal password=${secret:LAKE_PG}?data_path=s3://bucket/lake/
    ducklake:/srv/lake/meta.ducklake

Options: ``data_path`` (where a new lake keeps its files; an existing lake remembers its own), ``schema``
(default ``main``), and for an ``s3://``/``gs://`` data path the object-store credentials (``key_id``,
``secret``, ``session_token``, ``region``, ``endpoint``, ``use_ssl``, ``url_style``; with none, ``s3://``
uses the AWS credential chain), read exactly as an object-store Spout reads them.

Every source works. A merge Trickle's changelog window is applied by its key, an append Trickle's window is
inserted, and an overwrite table is replaced whole when its version advances, each in one transaction with
the watermark (shared with Postgres in :mod:`.attached`). The worker delivers all of a run's tables in one
:meth:`DuckLakeEgressDriver.batch`, so lake readers see one snapshot per run, labelled with the Pond.

The driver never migrates a catalog (no ``AUTOMATIC_MIGRATION``), so a DuckLake 1.0 lake stays readable by
its other readers. Compaction and snapshot expiry are the lake owner's to run.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import parse_qsl, urlencode

from . import credentials
from .attached import AttachedCatalogDriver, qi, qs
from .base import Capabilities
from .destination import Destination

_ALIAS = "duckstring_egress_lake"
_CREDENTIALS = ("key_id", "secret", "session_token", "region", "endpoint", "use_ssl", "url_style")
_OPTIONS = {"data_path", "schema", *_CREDENTIALS}


class DuckLakeEgressDriver(AttachedCatalogDriver):
    SCHEMES = ("ducklake",)

    def capabilities(self) -> Capabilities:
        return Capabilities(supports_delta=True, supports_delete=True, transactional=True, requires_pk=False)

    # ─── the destination ─────────────────────────────────────────────────────

    def _parts(self) -> tuple[str, dict[str, str]]:
        """``(attach string, options)``: the Spout's options taken out of the trailing query, the rest left
        on the attach string. Split before resolving, so a credential holding a ``?`` can't move the split."""
        meta, _, query = self.dest.raw.partition("?")
        pairs = parse_qsl(query, keep_blank_values=True)
        options = {k.lower(): credentials.resolve(v) for k, v in pairs if k.lower() in _OPTIONS}
        rest = [(k, v) for k, v in pairs if k.lower() not in _OPTIONS]
        attach = credentials.resolve(meta + ("?" + urlencode(rest, safe="${}:") if rest else ""))
        return attach, options

    def _schema(self) -> str:
        return self._parts()[1].get("schema", "main")

    def _redact(self, message: str) -> str:
        """A DuckLake error with every credential masked: resolved references, ``password=`` values and a
        URL's user-info password. Its own detail (a missing file, a catalog version) is worth showing."""
        for ref in credentials.references(self.dest.raw):
            try:
                value = credentials.resolve(f"${{{ref.scheme}:{ref.name}}}")
            except credentials.CredentialError:
                continue
            if value:
                message = message.replace(value, "***")
        message = re.sub(r"(?i)(password\s*=\s*)[^\s'\"&]+", r"\1***", message)
        return re.sub(r"(://[^:/@\s]+:)[^@\s]+@", r"\1***@", message)

    def _data_store(self, options: dict[str, str]):
        """The object-store driver for an ``s3://``/``gs://`` data path, for its credentials, else None."""
        from .object_store import ObjectStoreEgressDriver

        path = options.get("data_path", "")
        scheme = path.partition("://")[0].lower()
        if scheme not in ("s3", "gs"):
            return None
        creds = {k: options[k] for k in _CREDENTIALS if k in options}
        return ObjectStoreEgressDriver(Destination(scheme=scheme, raw=path + ("?" + urlencode(creds) if creds else "")))

    def _attach(self, con) -> str:
        if self._prefix is None:
            attach, options = self._parts()
            store = self._data_store(options)
            if store is not None:
                store._prepare_remote(con)  # httpfs + a secret DuckLake's file reads and writes use
            data_path = f" (DATA_PATH {qs(options['data_path'])})" if "data_path" in options else ""
            try:
                con.execute("INSTALL ducklake; LOAD ducklake")
                con.execute(f"ATTACH {qs(attach)} AS {qi(_ALIAS)}{data_path}")
            except Exception as exc:
                raise RuntimeError(f"could not attach the DuckLake destination: {self._redact(str(exc))}") from None
            self._prefix = f"{qi(_ALIAS)}.{qi(self._schema())}"
        return self._prefix

    def test_connection(self, con) -> None:
        """Attach (an absent catalog is created empty, as the first delivery would) and, for an object-store
        data path, list it, which surfaces a credential or endpoint problem before any delivery."""
        self._attach(con)
        con.execute("SELECT 1").fetchall()
        store = self._data_store(self._parts()[1])
        if store is not None:
            store.test_connection(con)

    # ─── one snapshot per run ────────────────────────────────────────────────

    @contextmanager
    def batch(self, con, *, pond: str, major: int, f: datetime):
        """Deliver a run's tables in one transaction, so the lake gains one snapshot for the run (or none, if
        nothing changed), with Duckstring as its author and the Pond and freshness in its commit details."""
        self._ensure_attached(con)
        con.execute("BEGIN TRANSACTION")
        self._in_batch = True
        try:
            info = json.dumps({"pond": pond, "major": major, "f": f.isoformat()})
            con.execute(f"CALL {qi(_ALIAS)}.set_commit_message('duckstring', {qs(f'{pond}@{major}')}, "
                        f"extra_info => {qs(info)})")
            yield
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            self._in_batch = False

    # ─── catalog specifics ───────────────────────────────────────────────────

    def _declares_pk(self) -> bool:
        return False  # DuckLake has no PRIMARY KEY or UNIQUE constraints; keys are applied by the delete

    def _alter_type(self, current: str, incoming: str) -> str | None:
        """A lake keeps DuckDB's types, so a source column that widened (the contract lets nothing else
        through within a major) is widened in the lake too."""
        from ..schema_contract import is_widening

        return incoming if is_widening(current, incoming) else None
