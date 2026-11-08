"""Databricks REST calls shared by the Databricks SQL Flock engine and the Databricks Spout
(plans/databricks-bigquery.md, part 2): SQL statements on a warehouse, and files in a Unity Catalog Volume.

Plain httpx against a handful of endpoints, as ``catchment/gcp_rest.py`` does for Google, rather than the
Databricks SDK. Authentication, in Databricks' own order of precedence:

1. a personal access token (``DATABRICKS_TOKEN``);
2. OAuth machine-to-machine with a Databricks service principal (``DATABRICKS_CLIENT_ID`` and
   ``DATABRICKS_CLIENT_SECRET``), which is what a Databricks App is given for its own service principal;
3. on Azure Databricks, a Microsoft Entra token from ``azure-identity``'s default chain (a managed identity, a
   service principal in ``AZURE_*``, an ``az`` login), so a Duck with a managed identity needs no secret.
"""

from __future__ import annotations

import os
import threading
import time
from urllib.parse import quote

ENTRA_RESOURCE = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"  # Azure Databricks, the same in every Azure environment
_TOKEN_MIN_LIFE_S = 5 * 60
SECRETS = ("DATABRICKS_TOKEN", "DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET")
# A Databricks result column's type name → the DuckDB type an empty result is written with.
DUCK_TYPES = {"BOOLEAN": "BOOLEAN", "BYTE": "TINYINT", "SHORT": "SMALLINT", "INT": "INTEGER", "LONG": "BIGINT",
              "FLOAT": "FLOAT", "DOUBLE": "DOUBLE", "STRING": "VARCHAR", "DATE": "DATE", "TIMESTAMP": "TIMESTAMPTZ",
              "TIMESTAMP_NTZ": "TIMESTAMP", "BINARY": "BLOB"}


class DatabricksError(RuntimeError):
    """A Databricks call or statement failed; the message is Databricks' own (it never carries a credential)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def normalise_host(host: str) -> str:
    host = host.strip().rstrip("/")
    return host if "://" in host else f"https://{host}"


class Client:
    """A workspace's REST API, authenticated from ``env`` (a mapping holding the credentials named above)."""

    def __init__(self, host: str, env, *, transport=None, timeout: float = 60.0):
        import httpx

        if not host:
            raise DatabricksError("no Databricks workspace host")
        self.host = normalise_host(host)
        self.env = env
        self._http = httpx.Client(timeout=timeout, transport=transport)
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires = 0.0

    # ── auth ──────────────────────────────────────────────────────────────────

    def _bearer(self) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._expires - _TOKEN_MIN_LIFE_S:
                return self._token
            pat = self.env.get("DATABRICKS_TOKEN")
            if pat:
                self._token, self._expires = pat, float("inf")
            elif self.env.get("DATABRICKS_CLIENT_ID") and self.env.get("DATABRICKS_CLIENT_SECRET"):
                self._token, self._expires = self._oauth_m2m()
            elif ".azuredatabricks.net" in self.host:
                self._token, self._expires = self._entra()
            else:
                raise DatabricksError("no Databricks credentials: set DATABRICKS_TOKEN, or DATABRICKS_CLIENT_ID and "
                                      "DATABRICKS_CLIENT_SECRET")
            return self._token

    def _oauth_m2m(self) -> tuple[str, float]:
        resp = self._http.post(f"{self.host}/oidc/v1/token",
                               data={"grant_type": "client_credentials", "scope": "all-apis"},
                               auth=(self.env["DATABRICKS_CLIENT_ID"], self.env["DATABRICKS_CLIENT_SECRET"]))
        if resp.status_code >= 300:
            raise DatabricksError(f"OAuth token request failed ({resp.status_code}): {_message(resp)}", resp.status_code)
        body = resp.json()
        return body["access_token"], time.monotonic() + float(body.get("expires_in", 3600))

    def _entra(self) -> tuple[str, float]:
        try:
            from azure.identity import DefaultAzureCredential
        except ImportError:
            raise DatabricksError("no Databricks credentials, and azure-identity (the azure extra) isn't installed "
                                  "for an Entra token") from None
        token = DefaultAzureCredential(exclude_interactive_browser_credential=True).get_token(
            f"{ENTRA_RESOURCE}/.default")
        return token.token, time.monotonic() + max(0.0, token.expires_on - time.time())

    def request(self, method: str, path: str, *, ok=(200, 201, 204), **kwargs):
        headers = {"Authorization": f"Bearer {self._bearer()}", **kwargs.pop("headers", {})}
        resp = self._http.request(method, f"{self.host}{path}", headers=headers, **kwargs)
        if resp.status_code not in ok:
            raise DatabricksError(f"{method} {path.split('?')[0]} failed ({resp.status_code}): {_message(resp)}",
                                  resp.status_code)
        return resp

    def close(self) -> None:
        self._http.close()

    # ── SQL statements ────────────────────────────────────────────────────────

    def execute(self, warehouse: str, statement: str, *, arrow: bool = False, timeout_s: float = 900,
                catalog: str | None = None, schema: str | None = None) -> dict:
        """Run one statement to completion on ``warehouse``; the final statement response. With ``arrow`` the
        result is left as Arrow chunks to download (:meth:`arrow_chunks`)."""
        body = {"warehouse_id": warehouse, "statement": statement, "wait_timeout": "30s",
                "on_wait_timeout": "CONTINUE"}
        if arrow:
            body.update(disposition="EXTERNAL_LINKS", format="ARROW_STREAM")
        if catalog:
            body["catalog"] = catalog
        if schema:
            body["schema"] = schema
        st = self.request("POST", "/api/2.0/sql/statements", json=body).json()
        deadline = time.monotonic() + timeout_s
        delay = 0.25
        while st.get("status", {}).get("state") in ("PENDING", "RUNNING"):
            if time.monotonic() > deadline:
                self.request("POST", f"/api/2.0/sql/statements/{st['statement_id']}/cancel")
                raise TimeoutError(f"Databricks statement {st['statement_id']} did not finish in {timeout_s:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2, 2.0)
            st = self.request("GET", f"/api/2.0/sql/statements/{st['statement_id']}").json()
        status = st.get("status", {})
        if status.get("state") != "SUCCEEDED":
            error = status.get("error", {})
            raise DatabricksError(f"Databricks statement {status.get('state', 'failed').lower()}: "
                                  f"{error.get('message', '')[:400]}")
        return st

    def arrow_chunks(self, st: dict):
        """Each result chunk of an ``arrow`` statement as a ``pyarrow.Table``, in order."""
        import pyarrow as pa

        manifest = st.get("manifest", {})
        links = list(st.get("result", {}).get("external_links") or [])
        for index in range(int(manifest.get("total_chunk_count") or 0)):
            link = next((x for x in links if x.get("chunk_index") == index), None)
            if link is None:
                chunk = self.request("GET", f"/api/2.0/sql/statements/{st['statement_id']}/result/chunks/{index}")
                links = chunk.json().get("external_links") or []
                link = links[0]
            resp = self._http.get(link["external_link"])  # presigned: no Authorization header
            if resp.status_code >= 300:
                raise DatabricksError(f"downloading result chunk {index} failed ({resp.status_code})", resp.status_code)
            yield pa.ipc.open_stream(resp.content).read_all()

    def save_result(self, st: dict, out_dir) -> dict:
        """Write an ``arrow`` statement's result into ``out_dir`` as Parquet files (one per chunk; an empty
        result as one empty file typed from the manifest). Returns ``{"rows", "chunks"}``."""
        import pyarrow.parquet as pq

        os.makedirs(out_dir, exist_ok=True)
        n = rows = 0
        for n, table in enumerate(self.arrow_chunks(st), start=1):
            pq.write_table(table, os.path.join(out_dir, f"part-{n:05d}.parquet"))
            rows += table.num_rows
        if n == 0:
            self._write_empty(st, out_dir)
        return {"rows": rows, "chunks": n}

    def _write_empty(self, st: dict, out_dir) -> None:
        import duckdb

        cols = []
        for c in self.schema_of(st):
            t = str(c.get("type_name", "STRING")).upper()
            duck = (f"DECIMAL({c.get('type_precision', 38)},{c.get('type_scale', 0)})" if t == "DECIMAL"
                    else DUCK_TYPES.get(t, "VARCHAR"))
            cols.append(f'CAST(NULL AS {duck}) AS "{c["name"]}"')
        path = os.path.join(out_dir, "part-00000.parquet").replace("'", "''")
        duckdb.connect().execute(f"COPY (SELECT {', '.join(cols) or '1 AS x'} WHERE false) TO '{path}' "
                                 "(FORMAT PARQUET)")

    def schema_of(self, st: dict) -> list[dict]:
        """The result's columns from a statement's manifest: ``[{name, type_text, ...}]``."""
        return list(st.get("manifest", {}).get("schema", {}).get("columns") or [])

    def rows(self, st: dict) -> list[list]:
        """An inline (JSON) result's rows, every value a string or ``None`` as Databricks sends them."""
        return list(st.get("result", {}).get("data_array") or [])

    # ── Volume files ──────────────────────────────────────────────────────────

    def upload(self, local_path, volume_path: str) -> None:
        with open(local_path, "rb") as fh:
            self.request("PUT", f"/api/2.0/fs/files{_path(volume_path)}?overwrite=true", content=fh,
                         headers={"Content-Type": "application/octet-stream"})

    def upload_bytes(self, data: bytes, volume_path: str) -> None:
        self.request("PUT", f"/api/2.0/fs/files{_path(volume_path)}?overwrite=true", content=data,
                     headers={"Content-Type": "application/octet-stream"})

    def head(self, volume_path: str) -> dict | None:
        """A file's ``{"size", "modified"}`` (``None`` when there is no file there; a directory isn't one)."""
        resp = self.request("HEAD", f"/api/2.0/fs/files{_path(volume_path)}", ok=(200, 404))
        if resp.status_code == 404:
            return None
        return {"size": int(resp.headers.get("content-length") or 0), "modified": resp.headers.get("last-modified")}

    def is_dir(self, volume_path: str) -> bool:
        resp = self.request("HEAD", f"/api/2.0/fs/directories{_path(volume_path)}", ok=(200, 404))
        return resp.status_code == 200

    def download(self, volume_path: str) -> bytes:
        return self.request("GET", f"/api/2.0/fs/files{_path(volume_path)}").content

    def download_to(self, volume_path: str, local_path) -> int:
        """Stream a file to ``local_path``; the bytes written."""
        n = 0
        with self._http.stream("GET", f"{self.host}/api/2.0/fs/files{_path(volume_path)}",
                               headers={"Authorization": f"Bearer {self._bearer()}"}) as resp:
            if resp.status_code >= 300:
                resp.read()
                raise DatabricksError(f"GET {volume_path} failed ({resp.status_code}): {_message(resp)}",
                                      resp.status_code)
            with open(local_path, "wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
                    n += len(chunk)
        return n

    def delete_file(self, volume_path: str) -> None:
        self.request("DELETE", f"/api/2.0/fs/files{_path(volume_path)}", ok=(200, 204, 404))

    def mkdirs(self, volume_path: str) -> None:
        self.request("PUT", f"/api/2.0/fs/directories{_path(volume_path)}", ok=(200, 201, 204, 409))

    def list_dir(self, volume_path: str) -> list[dict]:
        out, token = [], None
        while True:
            params = {"page_token": token} if token else {}
            resp = self.request("GET", f"/api/2.0/fs/directories{_path(volume_path)}", params=params, ok=(200, 404))
            if resp.status_code == 404:
                return out
            body = resp.json()
            out += body.get("contents") or []
            token = body.get("next_page_token")
            if not token:
                return out

    def remove_tree(self, volume_path: str) -> None:
        """Delete a Volume directory and everything under it (a no-op when it's gone)."""
        for entry in self.list_dir(volume_path):
            if entry.get("is_directory"):
                self.remove_tree(entry["path"])
            else:
                self.request("DELETE", f"/api/2.0/fs/files{_path(entry['path'])}", ok=(200, 204, 404))
        self.request("DELETE", f"/api/2.0/fs/directories{_path(volume_path)}", ok=(200, 204, 404))


def _path(volume_path: str) -> str:
    return quote("/" + volume_path.lstrip("/"))


def _message(resp) -> str:
    try:
        body = resp.json()
        return str(body.get("message") or body.get("error_description") or body.get("error") or resp.text)[:400]
    except ValueError:
        return resp.text[:400]


def sql_string(value: str) -> str:
    """A Databricks SQL string literal."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def ident(name: str) -> str:
    """A Databricks SQL identifier, backtick-quoted."""
    return "`" + str(name).replace("`", "``") + "`"
