"""Unity Catalog Volumes through the Files API, for a machine that doesn't mount ``/Volumes`` (a Databricks App,
plans/databricks-bigquery.md part 5; a laptop). A ``/Volumes/...`` data root or state backup is a local path where
Volumes are mounted (a cluster, a job) and this filesystem everywhere else (``storage.volume_api``).

DuckDB never reads or writes it: the Catchment's own Ducks publish to local disk and Persist mirrors the files
here, and the Catchment restores every line's local publish from here when it starts (``dataplane.restore_published``),
so reads always find a local copy. That keeps the store to the filesystem operations the data plane needs.

Two measured behaviours of the Files API (2026-11-08) shape it:

- an upload replaces the file whole, but isn't atomic for a reader: a read during an overwrite can return a 200
  with an empty body. A read that comes back empty while the file reports a size is retried.
- there is no create-if-absent and no rename: ``mv`` is a copy and a delete, so it isn't atomic either. The data
  plane's single writer per line is what makes that safe.

Credentials are the Databricks client's (``databricks_rest``): ``DATABRICKS_HOST`` plus a token, OAuth
machine-to-machine (what an App is given) or an Entra token on Azure.
"""

from __future__ import annotations

import os
import time

from fsspec import AbstractFileSystem

from . import databricks_rest as dbx

_EMPTY_READ_RETRIES = 8
_UPLOAD_LIMIT = 5 * 1024**3  # the Files API takes a file up to 5 GiB in one upload


class VolumeFileSystem(AbstractFileSystem):
    """An fsspec filesystem over a workspace's Volumes. Paths are absolute ``/Volumes/catalog/schema/volume/...``."""

    protocol = "dbvolume"
    cachable = False

    def __init__(self, client: dbx.Client | None = None, env=None, **kwargs):
        super().__init__(**kwargs)
        if client is None:
            env = os.environ if env is None else env
            host = env.get("DATABRICKS_HOST")
            if not host:
                raise dbx.DatabricksError("/Volumes isn't mounted here, and reaching a Volume through the Files API "
                                          "needs DATABRICKS_HOST (a Databricks App sets it)")
            client = dbx.Client(host, env)
        self.client = client

    @classmethod
    def _strip_protocol(cls, path):
        path = str(path)
        if path.startswith("dbvolume://"):
            path = path[len("dbvolume://"):]
        return "/" + path.strip("/")

    # ── inspection ─────────────────────────────────────────────────────────────

    def ls(self, path, detail=True, **kwargs):
        path = self._strip_protocol(path)
        entries = self.client.list_dir(path)
        if not entries and not self.client.is_dir(path):
            if self.client.head(path) is not None:  # a file lists as itself, as fsspec expects
                info = self.info(path)
                return [info] if detail else [info["name"]]
            raise FileNotFoundError(path)
        out = [{"name": "/" + e["path"].strip("/"), "type": "directory" if e.get("is_directory") else "file",
                "size": int(e.get("file_size") or 0)} for e in entries]
        return out if detail else [e["name"] for e in out]

    def info(self, path, **kwargs):
        path = self._strip_protocol(path)
        head = self.client.head(path)
        if head is not None:
            return {"name": path, "type": "file", "size": head["size"], "modified": head["modified"]}
        if self.client.is_dir(path):
            return {"name": path, "type": "directory", "size": 0}
        raise FileNotFoundError(path)

    # ── reads ──────────────────────────────────────────────────────────────────

    def cat_file(self, path, start=None, end=None, **kwargs):
        path = self._strip_protocol(path)
        data = self._read(path)
        return data[start:end] if start is not None or end is not None else data

    def _read(self, path: str) -> bytes:
        """The file's bytes, retrying a read that comes back empty while the file reports a size (an overwrite in
        flight)."""
        try:
            data = self.client.download(path)
        except dbx.DatabricksError as exc:
            if exc.status == 404:
                raise FileNotFoundError(path) from None
            raise
        for attempt in range(_EMPTY_READ_RETRIES):
            if data:
                return data
            head = self.client.head(path)
            if head is None:
                raise FileNotFoundError(path)
            if head["size"] == 0:
                return data
            time.sleep(0.1 * (attempt + 1))
            data = self.client.download(path)
        return data

    def get_file(self, rpath, lpath, **kwargs):
        rpath = self._strip_protocol(rpath)
        if self.isdir(rpath):
            os.makedirs(lpath, exist_ok=True)
            return
        os.makedirs(os.path.dirname(os.path.abspath(lpath)), exist_ok=True)
        for attempt in range(_EMPTY_READ_RETRIES):
            try:
                n = self.client.download_to(rpath, lpath)
            except dbx.DatabricksError as exc:
                if exc.status == 404:
                    raise FileNotFoundError(rpath) from None
                raise
            if n:
                return
            head = self.client.head(rpath)
            if head is None or head["size"] == 0:
                return
            time.sleep(0.1 * (attempt + 1))

    def _open(self, path, mode="rb", **kwargs):
        if "r" not in mode:
            raise NotImplementedError("write a Volume file with pipe_file or put_file")
        import io

        return io.BytesIO(self.cat_file(path))

    # ── writes ─────────────────────────────────────────────────────────────────

    def pipe_file(self, path, value, **kwargs):
        if len(value) > _UPLOAD_LIMIT:
            raise ValueError(f"{path}: the Files API takes a file up to 5 GiB")
        self.client.upload_bytes(bytes(value), self._strip_protocol(path))

    def put_file(self, lpath, rpath, **kwargs):
        rpath = self._strip_protocol(rpath)
        if os.path.isdir(lpath):
            self.client.mkdirs(rpath)
            return
        if os.path.getsize(lpath) > _UPLOAD_LIMIT:
            raise ValueError(f"{lpath}: the Files API takes a file up to 5 GiB")
        self.client.upload(lpath, rpath)

    def mkdir(self, path, create_parents=True, **kwargs):
        self.client.mkdirs(self._strip_protocol(path))

    def makedirs(self, path, exist_ok=False):
        self.client.mkdirs(self._strip_protocol(path))

    def rm_file(self, path):
        path = self._strip_protocol(path)
        if self.client.head(path) is None:
            raise FileNotFoundError(path)
        self.client.delete_file(path)

    def rm(self, path, recursive=False, maxdepth=None):
        for p in [path] if isinstance(path, str) else path:
            p = self._strip_protocol(p)
            if self.client.head(p) is not None:
                self.client.delete_file(p)
            elif self.client.is_dir(p):
                if not recursive:
                    raise IsADirectoryError(p)
                self.client.remove_tree(p)
            else:
                raise FileNotFoundError(p)

    def cp_file(self, path1, path2, **kwargs):
        self.pipe_file(path2, self._read(self._strip_protocol(path1)))

    def mv(self, path1, path2, recursive=False, maxdepth=None, **kwargs):
        """A copy then a delete: the Files API has no rename."""
        path1, path2 = self._strip_protocol(path1), self._strip_protocol(path2)
        if self.client.is_dir(path1):
            for entry in self.ls(path1):
                self.mv(entry["name"], path2 + entry["name"][len(path1):], recursive=True)
            self.client.remove_tree(path1)
            return
        self.cp_file(path1, path2)
        self.client.delete_file(path1)
