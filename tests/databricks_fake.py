"""A fake Databricks workspace for offline tests (an httpx mock transport): Volume files kept in memory, OAuth, and
the Statement Execution API running each statement on one DuckDB connection, so tables persist between statements.

A statement is translated from Databricks SQL to DuckDB with sqlglot, ``read_files`` over a Volume path becoming
``read_parquet`` over the uploads. The few Databricks statements DuckDB lacks are rewritten first: ``INSERT
OVERWRITE``, ``INSERT INTO ... REPLACE WHERE``, ``DESCRIBE QUERY``/``DESCRIBE TABLE`` and ``ADD COLUMNS``. Results
come back as Arrow chunks behind presigned links, or inline as JSON rows.
"""

from __future__ import annotations

import io
import json
import os
import re
import tempfile

import duckdb
import httpx
import pyarrow as pa

HOST = "https://adb-1.2.azuredatabricks.net"


class FakeWorkspace:
    """Enough of a Databricks workspace for the engine: Volume files, statements, result chunks, OAuth."""

    def __init__(self, *, chunk_rows: int = 2, pending_polls: int = 1, catalogs=("lake",)):
        self.con = duckdb.connect()
        self.con.execute("SET TimeZone = 'UTC'")
        for name in catalogs:
            self.con.execute(f"ATTACH ':memory:' AS {name}")
        self.files: dict[str, bytes] = {}
        self.statements: dict[str, dict] = {}
        self.executed: list[str] = []
        self.auth: list[str] = []
        self.chunk_rows = chunk_rows
        self.pending_polls = pending_polls
        self._tmp = tempfile.mkdtemp()

    def transport(self):
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "links.example":
            assert "authorization" not in request.headers  # presigned: never send the bearer
            return httpx.Response(200, content=self.chunks[path])
        if path == "/oidc/v1/token":
            return httpx.Response(200, json={"access_token": "m2m-token", "expires_in": 3600})
        self.auth.append(request.headers.get("authorization", ""))
        if path.startswith("/api/2.0/fs/files/"):
            key = path[len("/api/2.0/fs/files"):]
            if request.method == "PUT":
                self.files[key] = request.read()
                return httpx.Response(204)
            if request.method == "DELETE":
                return httpx.Response(204 if self.files.pop(key, None) is not None else 404)
        if path.startswith("/api/2.0/fs/directories/"):
            key = path[len("/api/2.0/fs/directories"):].rstrip("/")
            if request.method == "DELETE":
                return httpx.Response(204)
            children = {}
            for f in self.files:
                if f.startswith(key + "/"):
                    head = f[len(key) + 1:].split("/", 1)
                    children[head[0]] = len(head) > 1
            if not children:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json={"contents": [
                {"path": f"{key}/{n}", "is_directory": d} for n, d in sorted(children.items())]})
        if path == "/api/2.0/sql/statements" and request.method == "POST":
            return self._run(json.loads(request.read()))
        m = re.fullmatch(r"/api/2.0/sql/statements/([^/]+)", path)
        if m:
            st = self.statements[m.group(1)]
            st["_polls"] -= 1
            return httpx.Response(200, json=self._view(st))
        m = re.fullmatch(r"/api/2.0/sql/statements/([^/]+)/result/chunks/(\d+)", path)
        if m:
            st = self.statements[m.group(1)]
            return httpx.Response(200, json={"external_links": [st["_links"][int(m.group(2))]]})
        return httpx.Response(404, json={"message": f"no route {request.method} {path}"})

    def _run(self, body):
        sql = body["statement"]
        self.executed.append(sql)

        def files(m):
            prefix = m.group(1).rstrip("/") + "/"
            local = []
            for name, data in sorted(self.files.items()):
                if name.startswith(prefix) and name.endswith(".parquet"):
                    p = os.path.join(self._tmp, name.strip("/").replace("/", "_"))
                    with open(p, "wb") as fh:
                        fh.write(data)
                    local.append(p)
            return "read_parquet([" + ", ".join(f"'{p}'" for p in local) + "])"

        try:
            table = self._statement(sql, files)
        except duckdb.Error as exc:
            st = {"statement_id": f"s{len(self.statements)}", "_polls": 0,
                  "status": {"state": "FAILED", "error": {"message": str(exc)}}}
            self.statements[st["statement_id"]] = st
            return httpx.Response(200, json=self._view(st))
        sid = f"s{len(self.statements)}"
        links, self.chunks = [], getattr(self, "chunks", {})
        for i, batch in enumerate(table.to_batches(max_chunksize=self.chunk_rows) if table.num_rows else []):
            sink = io.BytesIO()
            with pa.ipc.new_stream(sink, table.schema) as w:
                w.write_batch(batch)
            self.chunks[f"/{sid}/{i}"] = sink.getvalue()
            links.append({"chunk_index": i, "external_link": f"https://links.example/{sid}/{i}"})
        columns = [{"name": f.name, "type_name": "LONG" if pa.types.is_integer(f.type) else "STRING"}
                   for f in table.schema]
        st = {"statement_id": sid, "_polls": self.pending_polls, "_links": links,
              "status": {"state": "SUCCEEDED"},
              "manifest": {"total_chunk_count": len(links), "schema": {"columns": columns}}}
        if body.get("disposition") == "EXTERNAL_LINKS":
            st["result"] = {"external_links": links[:1]}
        else:
            rows = table.to_pylist()
            st["result"] = {"data_array": [[None if v is None else str(v) for v in r.values()] for r in rows]}
        self.statements[sid] = st
        return httpx.Response(200, json=self._view(st))

    def _view(self, st):
        out = {k: v for k, v in st.items() if not k.startswith("_")}
        if st.get("_polls", 0) > 0:
            out = {"statement_id": st["statement_id"], "status": {"state": "RUNNING"}}
        return out



    def _statement(self, sql: str, files) -> pa.Table:
        """Run one Databricks statement on the persistent connection; its result as Arrow."""
        import sqlglot

        def duck(text: str) -> str:
            out = sqlglot.transpile(text, read="databricks", write="duckdb")[0]
            return re.sub(r"READ_FILES\('([^']+)'[^)]*\)", files, out, flags=re.IGNORECASE)

        m = re.fullmatch(r"INSERT OVERWRITE (\S+) (\([^)]*\)) (SELECT .*)", sql, flags=re.S)
        if m:
            target = duck(f"SELECT * FROM {m.group(1)}").removeprefix("SELECT * FROM ")
            self.con.execute(f"DELETE FROM {target}")
            self.con.execute(f"INSERT INTO {target} {m.group(2).replace('`', chr(34))} {duck(m.group(3))}")
            return pa.table({})
        m = re.fullmatch(r"INSERT INTO (\S+) (\([^)]*\)) REPLACE WHERE (.*?) (SELECT .*)", sql, flags=re.S)
        if m:
            target = duck(f"SELECT * FROM {m.group(1)}").removeprefix("SELECT * FROM ")
            self.con.execute(duck(f"DELETE FROM {m.group(1)} WHERE {m.group(3)}"))
            self.con.execute(f"INSERT INTO {target} {m.group(2).replace('`', chr(34))} {duck(m.group(4))}")
            return pa.table({})
        m = re.fullmatch(r"DESCRIBE QUERY (.*)", sql, flags=re.S)
        if m:
            rows = self.con.execute(f"DESCRIBE {duck(m.group(1))}").fetchall()
            return pa.table({"col_name": [r[0] for r in rows], "data_type": [_dbx_type(r[1]) for r in rows]})
        m = re.fullmatch(r"DESCRIBE TABLE (\S+)", sql)
        if m:
            rows = self.con.execute(duck(f"DESCRIBE SELECT * FROM {m.group(1)}")).fetchall()
            return pa.table({"col_name": [r[0] for r in rows], "data_type": [_dbx_type(r[1]) for r in rows]})
        m = re.fullmatch(r"ALTER TABLE (\S+) ADD COLUMNS \((.*)\)", sql, flags=re.S)
        if m:
            target = duck(f"SELECT * FROM {m.group(1)}").removeprefix("SELECT * FROM ")
            for col in m.group(2).split(", "):
                name, _, typ = col.partition(" ")
                self.con.execute(f"ALTER TABLE {target} ADD COLUMN {name.replace('`', chr(34))} {_duck_type(typ)}")
            return pa.table({})
        text = duck(sql)
        # DuckDB's MERGE names target columns bare in UPDATE SET.
        text = re.sub(r"UPDATE SET (.*?) WHEN", lambda mm: "UPDATE SET " + re.sub(r'\bt\.', '', mm.group(1)) + " WHEN",
                      text, flags=re.S)
        rel = self.con.sql(text)  # a statement without a result runs here and returns None
        return rel.to_arrow_table() if rel is not None else pa.table({})

    def table(self, name: str) -> list[tuple]:
        return sorted(self.con.execute(f"SELECT * FROM {name}").fetchall(), key=repr)


_TO_DBX = {"BIGINT": "BIGINT", "INTEGER": "INT", "VARCHAR": "STRING", "DOUBLE": "DOUBLE", "BOOLEAN": "BOOLEAN",
           "DATE": "DATE", "TIMESTAMP": "TIMESTAMP_NTZ", "TIMESTAMP WITH TIME ZONE": "TIMESTAMP"}


def _dbx_type(t: str) -> str:
    return _TO_DBX.get(t, t if t.startswith("DECIMAL") else "STRING")


def _duck_type(t: str) -> str:
    return {"STRING": "VARCHAR", "INT": "INTEGER", "TIMESTAMP_NTZ": "TIMESTAMP"}.get(t, t)
