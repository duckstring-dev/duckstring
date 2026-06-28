"""Object-store egress driver — the baseline (see plans/egress.md "Object-store egress").

v1: write each egressed table as a **snapshot** Parquet file under the destination prefix
(``{prefix}/{table}.parquet``). ``supports_delta=False``, so the worker always ``write_full``s — the
simplest correct "land my Pond's output as Parquet over there".

- ``file://`` — local path, written atomically (tmp + ``os.replace``).
- ``s3://`` / ``gs://`` — via DuckDB ``httpfs`` + the secret manager. Credentials come from the URI
  query (``?key_id=${env:..}&secret=${env:..}&region=..``, resolved at egress time); with none given,
  ``s3://`` falls back to the AWS credential chain (env / instance profile). A single PUT / completed
  multipart upload is effectively atomic, so the target key is written directly.

The incremental per-run-parts / Iceberg-in-bucket layout is the next step.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import credentials
from .base import Capabilities
from .destination import Destination

_SECRET = "__duckstring_egress"  # transient per-connection secret the COPY uses


def _safe_table(name: str) -> str:
    if not name or name in (".", "..") or "/" in name or "\\" in name or os.sep in name:
        raise ValueError(f"unsafe table name for an object-store path: {name!r}")
    return name


def _q(value: str) -> str:
    """A single-quoted SQL string literal (doubling embedded quotes)."""
    return "'" + value.replace("'", "''") + "'"


class ObjectStoreEgressDriver:
    SCHEMES = ("file", "s3", "gs")

    def __init__(self, dest: Destination):
        self.dest = dest

    def capabilities(self) -> Capabilities:
        return Capabilities(supports_delta=False, supports_delete=False, transactional=False)

    def ensure(self, *, table: str, schema: dict | None, pk: list[str] | None) -> None:
        if self.dest.scheme == "file":
            self._file_base().mkdir(parents=True, exist_ok=True)  # buckets must already exist for s3/gs

    def write_full(self, con, relation, *, table: str, pk: list[str] | None, f: datetime) -> None:
        t = _safe_table(table)
        if self.dest.scheme == "file":
            base = self._file_base()
            base.mkdir(parents=True, exist_ok=True)
            target = base / f"{t}.parquet"
            tmp = base / f".{t}.{os.getpid()}.tmp.parquet"  # same dir → atomic os.replace
            try:
                relation.write_parquet(str(tmp))
                os.replace(tmp, target)
            finally:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
        else:
            self._prepare_remote(con)
            relation.write_parquet(self._remote_target(t))  # httpfs upload — atomic on completion

    def apply_delta(self, delta, *, table: str, pk: list[str] | None, f: datetime) -> None:
        raise NotImplementedError("object-store egress is snapshot-only in v1 (no apply_delta)")

    # ─── file:// ─────────────────────────────────────────────────────────────

    def _file_base(self) -> Path:
        """The local directory for a ``file://`` destination, ``${env:NAME}`` resolved (at egress time,
        never stored/logged)."""
        u = urlparse(credentials.resolve(self.dest.raw))
        if u.netloc and u.netloc not in ("", "localhost"):
            raise ValueError(
                f"file:// destination must be an absolute local path (file:///path); got host {u.netloc!r}"
            )
        return Path(u.path)

    # ─── s3:// / gs:// ───────────────────────────────────────────────────────

    def _remote_target(self, table: str) -> str:
        """``{scheme}://{bucket}/{prefix}/{table}.parquet`` — the target key, with no credential query
        (those live in the secret), so an error message can never echo a secret."""
        u = urlparse(credentials.resolve(self.dest.raw))
        if not u.netloc:
            raise ValueError(f"{self.dest.scheme}:// destination needs a bucket: {self.dest.scheme}://bucket/prefix")
        prefix = u.path.strip("/")
        key = f"{prefix}/{table}.parquet" if prefix else f"{table}.parquet"
        return f"{self.dest.scheme}://{u.netloc}/{key}"

    def _secret_sql(self) -> str:
        """A ``CREATE OR REPLACE SECRET`` from the destination's query params (``${env}`` resolved).
        s3 with no key falls back to ``credential_chain``; gs requires HMAC key_id + secret."""
        u = urlparse(credentials.resolve(self.dest.raw))
        q = {k.lower(): v[0] for k, v in parse_qs(u.query).items()}
        clauses: list[str] = []

        def add(qkey: str, name: str) -> None:
            if qkey in q:
                clauses.append(f"{name} {_q(q[qkey])}")

        if self.dest.scheme == "gs":
            if "key_id" not in q or "secret" not in q:
                raise ValueError(
                    "gs:// egress needs HMAC credentials in the URI: ?key_id=${env:...}&secret=${env:...}"
                )
            clauses += ["TYPE gcs"]
            add("key_id", "KEY_ID")
            add("secret", "SECRET")
        else:  # s3
            clauses.append("TYPE s3")
            if "key_id" in q or "secret" in q:
                add("key_id", "KEY_ID")
                add("secret", "SECRET")
                add("session_token", "SESSION_TOKEN")
            else:
                clauses.append("PROVIDER credential_chain")
            add("region", "REGION")
            add("endpoint", "ENDPOINT")
            add("url_style", "URL_STYLE")
            if "use_ssl" in q:
                clauses.append(f"USE_SSL {'true' if q['use_ssl'].lower() in ('1', 'true', 'yes') else 'false'}")
        return f"CREATE OR REPLACE SECRET {_SECRET} (" + ", ".join(clauses) + ")"

    def _prepare_remote(self, con) -> None:
        con.execute("INSTALL httpfs; LOAD httpfs")  # offline/install errors surface (no credentials in them)
        secret_sql = self._secret_sql()  # ValueError (gs creds) / CredentialError (missing env) surface — safe
        try:
            con.execute(secret_sql)
        except Exception:
            # The statement carries resolved credentials; never let a driver error echo them.
            raise RuntimeError(
                f"failed to configure {self.dest.scheme} credentials (check the destination's auth params)"
            ) from None
