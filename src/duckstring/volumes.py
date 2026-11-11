"""Unity Catalog Volumes through the Files API, for a machine that doesn't mount ``/Volumes`` (a Databricks App,
plans/databricks-bigquery.md part 5; a laptop). A ``/Volumes/...`` data root or state backup is a local path where
Volumes are mounted (a cluster, a job) and this filesystem everywhere else (``storage.volume_api``).

DuckDB never reads or writes it: the Catchment's own Ducks publish to local disk and Persist mirrors the files
here, and the Catchment restores every line's local publish from here when it starts (``dataplane.restore_published``),
so reads always find a local copy. That keeps the store to the filesystem operations the data plane needs.

Measured behaviours of the Files API (2026-11-08 and 2026-11-09) shape it:

- a file is visible from the moment its upload starts: listed, and answering HEAD, with size 0 and an empty body
  until the upload completes. An overwrite is the same: the old content is gone at once, so a reader during an
  overwrite (or after a writer that died in one) finds an empty file. Duckstring's Parquet files are written
  once under names a commit point (a sidecar) refers to, so only the files it overwrites need care: those are
  written through a **shadow** (``.{name}.duckstring-next``) holding the new content first, then the file, then
  the shadow is deleted. A read that finds the file empty takes the shadow's content, so a reader always gets
  the old content or the new, and a writer that dies part way leaves one of them whole. Shadows are hidden from
  listings.
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

_EMPTY_READ_RETRIES = 8  # a Parquet file is never empty, so an empty read is one in flight
_UPLOAD_LIMIT = 5 * 1024**3  # the Files API takes a file up to 5 GiB in one upload
SHADOW_SUFFIX = ".duckstring-next"


def shadowed(path: str) -> bool:
    """Whether writes to ``path`` go through a shadow: every file but Parquet, which Duckstring writes once under
    a name a commit point refers to (a sidecar, a manifest)."""
    return not path.endswith(".parquet")


def shadow_of(path: str) -> str:
    head, _, name = path.rpartition("/")
    return f"{head}/.{name}{SHADOW_SUFFIX}"


def _is_shadow(path: str) -> bool:
    name = path.rstrip("/").rpartition("/")[2]
    return name.startswith(".") and name.endswith(SHADOW_SUFFIX)


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
                "size": int(e.get("file_size") or 0)} for e in entries if not _is_shadow(e["path"])]
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
        """The file's bytes. An empty read is an upload in flight, or one a writer died in: a shadowed file takes
        its shadow's content, and a Parquet file (never empty) is read again."""
        def fetch(p):
            try:
                return self.client.download(p)
            except dbx.DatabricksError as exc:
                if exc.status == 404:
                    return None
                raise

        return self._settled(path, fetch)

    def _settled(self, path: str, fetch):
        """``fetch(path)``'s content once it isn't an overwrite in flight (see :meth:`_read`). ``fetch`` returns
        the content (anything with a length) or None for a missing file."""
        data = fetch(path)
        if data is None:
            raise FileNotFoundError(path)
        if shadowed(path):
            for _ in range(2):  # the file, then (empty) the shadow, then the file again: an upload just finished
                if len(data):
                    return data
                shadow = fetch(shadow_of(path))
                if shadow is not None and len(shadow):
                    return shadow
                data = fetch(path)
                if data is None:
                    raise FileNotFoundError(path)
            return data  # empty, with no shadow: a file that really is empty
        for attempt in range(_EMPTY_READ_RETRIES):
            if len(data):
                return data
            time.sleep(0.1 * (attempt + 1))
            data = fetch(path)
            if data is None:
                raise FileNotFoundError(path)
        return data

    def get_file(self, rpath, lpath, **kwargs):
        rpath = self._strip_protocol(rpath)
        if self.isdir(rpath):
            os.makedirs(lpath, exist_ok=True)
            return
        os.makedirs(os.path.dirname(os.path.abspath(lpath)), exist_ok=True)

        class _Written:
            def __init__(self, n):
                self.n = n

            def __len__(self):
                return self.n

        def fetch(p):
            try:
                return _Written(self.client.download_to(p, lpath))
            except dbx.DatabricksError as exc:
                if exc.status == 404:
                    return None
                raise

        self._settled(rpath, fetch)  # each attempt streams into lpath; the last one is what's kept

    def _open(self, path, mode="rb", **kwargs):
        if "r" not in mode:
            raise NotImplementedError("write a Volume file with pipe_file or put_file")
        import io

        return io.BytesIO(self.cat_file(path))

    # ── writes ─────────────────────────────────────────────────────────────────

    def pipe_file(self, path, value, **kwargs):
        if len(value) > _UPLOAD_LIMIT:
            raise ValueError(f"{path}: the Files API takes a file up to 5 GiB")
        data = bytes(value)
        self._write(self._strip_protocol(path), lambda p: self.client.upload_bytes(data, p),
                    shadow=kwargs.get("shadow"))

    def put_file(self, lpath, rpath, **kwargs):
        rpath = self._strip_protocol(rpath)
        if os.path.isdir(lpath):
            self.client.mkdirs(rpath)
            return
        if os.path.getsize(lpath) > _UPLOAD_LIMIT:
            raise ValueError(f"{lpath}: the Files API takes a file up to 5 GiB")
        self._write(rpath, lambda p: self.client.upload(lpath, p))

    def _write(self, path: str, upload, shadow: bool | None = None) -> None:
        """``upload(path)``, through a shadow for a file Duckstring overwrites (see the module docstring);
        ``shadow=False`` for one written once."""
        if not (shadowed(path) if shadow is None else shadow):
            return upload(path)
        shadow = shadow_of(path)
        upload(shadow)
        upload(path)
        self.client.delete_file(shadow)

    def mkdir(self, path, create_parents=True, **kwargs):
        self.client.mkdirs(self._strip_protocol(path))

    def makedirs(self, path, exist_ok=False):
        self.client.mkdirs(self._strip_protocol(path))

    def rm_file(self, path):
        path = self._strip_protocol(path)
        if self.client.head(path) is None:
            raise FileNotFoundError(path)
        self.client.delete_file(path)
        if shadowed(path):
            self.client.delete_file(shadow_of(path))  # one a writer died beside (a missing one is fine)

    def rm(self, path, recursive=False, maxdepth=None):
        for p in [path] if isinstance(path, str) else path:
            p = self._strip_protocol(p)
            if self.client.head(p) is not None:
                self.rm_file(p)
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
