"""A Postgres wire-protocol front door onto the serving core (plans/data-serving.md): the default,
universal-reach adapter (psql, Power BI, Metabase, Tableau, any pg driver). A thin translator: it speaks
enough of the v3 protocol to run the client's SQL on the sandboxed serving connection and stream the
result back. **DuckDB itself answers the `pg_catalog`/`information_schema` introspection** the tools
issue (over the serving connection's schemas), except a driver's type loading, which needs Postgres's own
type OIDs (`pg_types.catalog_answer`).

Auth: the pg password is a Duckstring API key → its access level (open mode → full). Read/demand →
the sandboxed connection (serviceable-only, no external access); full → the ops connection. A Tap's key
(plans/taps.md) gets the Tap's own connection, and each query it runs applies the Tap's policy. An open
Catchment never asks for a password, so there a connection can't be told apart as a Tap's.

Results go in the format each column is asked for (the extended flow's Bind; Npgsql, which Power BI uses,
asks for binary), with the Postgres type `pg_types.pg_type` gives each DuckDB type; the simple flow is text.
Parameters are decoded by the types the client declared at Parse. In the extended flow a portal's query runs
once, at Describe (or at Execute when it wasn't described), and Execute streams the rows it holds.

TLS: given an `ssl.SSLContext`, the server accepts a client's SSLRequest and refuses a connection that didn't
make one, since the password is an API key.
"""

from __future__ import annotations

import logging
import socket
import ssl
import struct
import threading
from dataclasses import dataclass, field

from . import auth
from .pg_types import TEXT, PgType, catalog_answer, decode_param, encode, pg_type, typmod

log = logging.getLogger("duckstring.pg")

_SSL_REQUEST, _GSS_REQUEST, _CANCEL_REQUEST = 80877103, 80877104, 80877102
_BATCH_ROWS = 1000

# Sent once a connection is up. Npgsql checks integer_datetimes before reading a timestamp in binary.
_SERVER_PARAMS = {
    "server_version": "14.0 (duckstring)", "server_encoding": "UTF8", "client_encoding": "UTF8",
    "DateStyle": "ISO, MDY", "integer_datetimes": "on", "standard_conforming_strings": "on",
    "TimeZone": "UTC", "IntervalStyle": "postgres", "is_superuser": "off",
}


# ─── wire message writers ────────────────────────────────────────────────────────

def tls_context(cert: str | None, key: str | None) -> ssl.SSLContext | None:
    """A server TLS context from a PEM certificate (chain) and its key, or None when no certificate is given.
    The key defaults to the certificate file, for a PEM holding both."""
    if not cert:
        if key:
            raise ValueError("DUCKSTRING_SERVE_PG_TLS_KEY is set without DUCKSTRING_SERVE_PG_TLS_CERT")
        return None
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key or None)
    return ctx


def _msg(tag: bytes, body: bytes) -> bytes:
    return tag + struct.pack("!I", len(body) + 4) + body


def _auth_ok() -> bytes:
    return _msg(b"R", struct.pack("!I", 0))


def _auth_cleartext() -> bytes:
    return _msg(b"R", struct.pack("!I", 3))


def _param_status(k: str, v: str) -> bytes:
    return _msg(b"S", k.encode() + b"\x00" + v.encode() + b"\x00")


def _ready(status: bytes = b"I") -> bytes:
    return _msg(b"Z", status)


@dataclass
class _Shape:
    """A result's columns as sent: names, Postgres types, DuckDB types (for typmods) and per-column binary flags."""
    names: list[str]
    types: list[PgType]
    duck_types: list[str]
    binary: list[bool] = field(default_factory=list)

    @classmethod
    def of(cls, names, duck_types) -> _Shape:
        duck_types = [str(t) for t in duck_types]
        return cls(list(names), [pg_type(t) for t in duck_types], duck_types, [False] * len(names))

    def with_formats(self, codes: list[int]) -> _Shape:
        """Apply Bind's result format codes: none = all text, one = all columns, else one per column."""
        n = len(self.names)
        flags = [False] * n if not codes else [codes[0] == 1] * n if len(codes) == 1 else [c == 1 for c in codes[:n]]
        return _Shape(self.names, self.types, self.duck_types, flags)


def _row_description(shape: _Shape) -> bytes:
    body = struct.pack("!H", len(shape.names))
    for name, t, dt, binary in zip(shape.names, shape.types, shape.duck_types, shape.binary, strict=True):
        body += name.encode("utf-8") + b"\x00"
        body += struct.pack("!IhIhih", 0, 0, t.oid, t.size, typmod(dt), 1 if binary else 0)
    return _msg(b"T", body)


def _data_row(row, shape: _Shape) -> bytes:
    body = struct.pack("!H", len(row))
    for v, t, binary in zip(row, shape.types, shape.binary, strict=True):
        b = encode(t, v, binary)
        body += struct.pack("!i", -1) if b is None else (struct.pack("!I", len(b)) + b)
    return _msg(b"D", body)


def _command_complete(tag: str) -> bytes:
    return _msg(b"C", tag.encode() + b"\x00")


def _error(message: str, code: str = "42000") -> bytes:
    body = b"S" + b"ERROR\x00" + b"C" + code.encode() + b"\x00" + b"M" + message.encode("utf-8") + b"\x00" + b"\x00"
    return _msg(b"E", body)


def _tag_for(sql: str, n: int) -> str:
    verb = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    if verb in ("SELECT", "WITH", "VALUES", "TABLE", "PRAGMA", "SHOW", "DESCRIBE", "SUMMARIZE", "FROM", ""):
        return f"SELECT {n}"
    return f"INSERT 0 {n}" if verb == "INSERT" else f"{verb} {n}"


# Session and transaction commands, with the tag Postgres completes each with. Every statement runs on a cursor of
# its own over the read-only serving connection, so there is no session state for one to change or reset (and a
# client can't change DuckDB's settings with SET). Drivers send them as a matter of course: Npgsql resets a pooled
# connection with DISCARD ALL, or with SET SESSION AUTHORIZATION DEFAULT;RESET ALL;UNLISTEN *;... once it has
# prepared statements; JDBC sets extra_float_digits; SQL clients open transactions.
_SESSION_TAGS = {"SET": "SET", "RESET": "RESET", "UNLISTEN": "UNLISTEN", "CLOSE": "CLOSE CURSOR",
                 "BEGIN": "BEGIN", "START": "START TRANSACTION", "COMMIT": "COMMIT", "END": "COMMIT",
                 "ROLLBACK": "ROLLBACK", "ABORT": "ROLLBACK"}


def _session_tag(sql: str) -> str | None:
    """The command tag a session or transaction command completes with, or None for any other statement."""
    words = sql.split(None, 2)
    if not words:
        return None
    verb = words[0].upper()
    if verb == "DISCARD":
        return "DISCARD " + (words[1].upper() if len(words) > 1 else "ALL")
    return _SESSION_TAGS.get(verb)


def _values_sql(columns: list[str], rows: list[tuple]) -> str:
    """A catalog answer as one DuckDB query over text values."""
    names = ['"' + c.replace('"', '""') + '"' for c in columns]
    if not rows:
        return "SELECT " + ", ".join(f"NULL::VARCHAR AS {n}" for n in names) + " WHERE false"
    def literal(v) -> str:
        return "NULL::VARCHAR" if v is None else "'" + str(v).replace("'", "''") + "'"

    lit = ", ".join("(" + ", ".join(literal(v) for v in r) + ")" for r in rows)
    return f"SELECT * FROM (VALUES {lit}) AS t({', '.join(names)})"


class _Result:
    """A query's outcome: its shape and an open cursor to stream rows from, or no rows (a command)."""

    def __init__(self, sql: str, cur=None, shape: _Shape | None = None, tag: str | None = None):
        self.sql, self.cur, self.shape, self.tag = sql, cur, shape, tag

    def batches(self):
        if self.cur is None:
            return
        while True:
            rows = self.cur.fetchmany(_BATCH_ROWS)
            if not rows:
                return
            yield rows


@dataclass
class _Portal:
    sql: str
    params: list
    formats: list[int]
    result: _Result | None = None       # run at Describe, streamed at Execute


class PgWireServer:
    kind = "pg"

    def __init__(self, driver, *, api_key: str | None = None, host: str = "127.0.0.1", port: int = 5433,
                 tls: ssl.SSLContext | None = None):
        self.driver = driver
        self.api_key = api_key
        self.host, self.port = host, port
        self.tls = tls
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> int:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self._running = True
        self._thread = threading.Thread(target=self._accept, name="ds-pg-wire", daemon=True)
        self._thread.start()
        log.info("pg-wire serving on %s:%s%s", self.host, self.port, " (TLS)" if self.tls else "")
        return self.port

    def stop(self) -> None:
        self._running = False
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass

    def _accept(self) -> None:
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    # ─── per-connection ───────────────────────────────────────────────────────────

    def _recv_exact(self, conn, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("client closed")
            buf += chunk
        return buf

    def _startup(self, conn) -> tuple[socket.socket, auth.Principal | None]:
        """Negotiate TLS and authenticate. Returns the connection to carry on with (wrapped when TLS was
        negotiated) and the principal, or None when the client is turned away."""
        secure = False
        while True:
            length = struct.unpack("!I", self._recv_exact(conn, 4))[0]
            body = self._recv_exact(conn, length - 4)
            code = struct.unpack("!I", body[:4])[0]
            if code == _SSL_REQUEST and self.tls is not None and not secure:
                conn.sendall(b"S")
                conn = self.tls.wrap_socket(conn, server_side=True)
                secure = True
                continue
            if code in (_SSL_REQUEST, _GSS_REQUEST):
                conn.sendall(b"N")
                continue
            if code == _CANCEL_REQUEST:
                return conn, None
            break
        if self.tls is not None and not secure:
            conn.sendall(_error("this server requires TLS: connect with SSL", "28000"))
            return conn, None

        con = getattr(self.driver, "db", None)
        if not auth.auth_configured(con, self.api_key):
            conn.sendall(_auth_ok())          # open mode → no password
            return conn, auth.Principal(level=auth.Level.FULL, is_duck=False)
        conn.sendall(_auth_cleartext())        # ask for the API key as the password
        tag = self._recv_exact(conn, 1)
        if tag != b"p":
            return conn, None
        plen = struct.unpack("!I", self._recv_exact(conn, 4))[0]
        pw = self._recv_exact(conn, plen - 4).rstrip(b"\x00").decode(errors="replace")
        principal = auth.principal_for_key(self.driver, self.api_key, pw)
        if principal is not None:
            conn.sendall(_auth_ok())          # password accepted
        return conn, principal

    def _cursor(self, principal: auth.Principal):
        from .serving import _cache

        sandboxed = principal.tap is not None or principal.level < auth.Level.FULL
        return _cache(self.driver).connection(self.driver, sandboxed=sandboxed, tap=principal.tap).cursor()

    def _run(self, principal: auth.Principal, sql: str, params=None, *, gate: bool = False) -> _Result:
        """Run one statement. ``gate`` (an executed query, not a Describe) applies a Tap's policy first."""
        from .taps import apply_tap

        tag = _session_tag(sql)
        if tag is not None:
            return _Result(sql, tag=tag)
        canned = catalog_answer(sql)
        if canned is None and gate and principal.tap is not None:
            apply_tap(self.driver, principal.tap)
        cur = self._cursor(principal)
        cur.execute(_values_sql(*canned) if canned else sql, (params or []) if canned is None else [])
        if not cur.description:
            return _Result(sql)
        return _Result(sql, cur, _Shape.of([d[0] for d in cur.description], [d[1] for d in cur.description]))

    def _shape(self, principal: auth.Principal, sql: str, nparams: int) -> _Shape | None:
        """A statement's columns without running it (Describe of a prepared statement). DuckDB binds a query
        lazily as a relation; one with parameters can't be bound without values, so it runs with NULLs."""
        if _session_tag(sql) is not None:
            return None
        canned = catalog_answer(sql)
        if canned is not None:
            return _Shape.of(canned[0], ["VARCHAR"] * len(canned[0]))
        if nparams == 0:
            try:
                rel = self._cursor(principal).sql(sql)
                return _Shape.of(rel.columns, rel.types) if rel is not None else None
            except Exception:
                pass
        return self._run(principal, sql, [None] * nparams).shape

    def _send_rows(self, conn, result: _Result, shape: _Shape | None) -> None:
        n = 0
        for rows in result.batches():
            conn.sendall(b"".join(_data_row(r, shape) for r in rows))
            n += len(rows)
        conn.sendall(_command_complete(result.tag or _tag_for(result.sql, n)))

    def _handle(self, conn) -> None:
        try:
            conn, principal = self._startup(conn)
            if principal is None:
                conn.sendall(_error("authentication failed", "28000"))
                conn.close()
                return
            conn.sendall(b"".join(_param_status(k, v) for k, v in _SERVER_PARAMS.items()))
            conn.sendall(_msg(b"K", struct.pack("!II", 0, 0)))  # BackendKeyData (unused)
            conn.sendall(_ready())

            statements: dict[str, tuple[str, list[int]]] = {}   # name → (sql, declared parameter OIDs)
            portals: dict[str, _Portal] = {}
            err = False  # in the extended flow, skip messages until the next Sync after an error
            while self._running:
                tag = self._recv_exact(conn, 1)
                length = struct.unpack("!I", self._recv_exact(conn, 4))[0]
                body = self._recv_exact(conn, length - 4)
                if tag == b"S":                                   # Sync → end of an extended batch
                    err = False
                    conn.sendall(_ready())
                    continue
                if err:
                    continue
                if tag == b"Q":                                   # simple query: text results
                    sql = body.split(b"\x00", 1)[0].decode("utf-8", "replace")
                    stmts = [s for s in _split(sql) if s.strip()]
                    if not stmts:
                        conn.sendall(_msg(b"I", b""))             # EmptyQueryResponse
                    for stmt in stmts:
                        try:
                            result = self._run(principal, stmt, gate=True)
                            if result.shape is not None:
                                conn.sendall(_row_description(result.shape))
                            self._send_rows(conn, result, result.shape)
                        except Exception as exc:
                            conn.sendall(_error(str(exc)))
                            break
                    conn.sendall(_ready())
                elif tag == b"P":                                 # Parse: name\0 query\0 int16 n, n × OID
                    name, q, tail = body.split(b"\x00", 2)
                    n = struct.unpack("!H", tail[:2])[0] if len(tail) >= 2 else 0
                    oids = list(struct.unpack(f"!{n}I", tail[2:2 + 4 * n])) if n else []
                    statements[name.decode()] = (q.decode("utf-8", "replace"), oids)
                    conn.sendall(_msg(b"1", b""))
                elif tag == b"B":                                 # Bind
                    portal, stmt, param_formats, raw, result_formats = _parse_bind(body)
                    sql, oids = statements.get(stmt, ("", []))
                    params = [None if v is None else decode_param(oids[i] if i < len(oids) else 0,
                                                                  _format_of(param_formats, i) == 1, v)
                              for i, v in enumerate(raw)]
                    portals[portal] = _Portal(sql, params, result_formats)
                    conn.sendall(_msg(b"2", b""))
                elif tag == b"D":                                 # Describe (kind byte + name)
                    kind, name = body[:1], body[1:].split(b"\x00", 1)[0].decode()
                    try:
                        if kind == b"S":
                            sql, oids = statements.get(name, ("", []))
                            nparams = _param_count(sql, oids)
                            declared = [o or TEXT.oid for o in oids] + [TEXT.oid] * (nparams - len(oids))
                            conn.sendall(_msg(b"t", struct.pack(f"!H{nparams}I", nparams, *declared[:nparams])))
                            shape = self._shape(principal, sql, nparams)
                        else:
                            p = portals.get(name) or _Portal("", [], [])
                            p.result = self._run(principal, p.sql, p.params, gate=True)
                            shape = p.result.shape.with_formats(p.formats) if p.result.shape else None
                        conn.sendall(_row_description(shape) if shape is not None else _msg(b"n", b""))
                    except Exception as exc:
                        conn.sendall(_error(str(exc)))
                        err = True
                elif tag == b"E":                                 # Execute: rows only (Describe sent the shape)
                    name = body.split(b"\x00", 1)[0].decode()
                    p = portals.get(name) or _Portal("", [], [])
                    try:
                        result = p.result or self._run(principal, p.sql, p.params, gate=True)
                        p.result = None                           # a re-Execute runs the query again
                        self._send_rows(conn, result, result.shape.with_formats(p.formats) if result.shape else None)
                    except Exception as exc:
                        conn.sendall(_error(str(exc)))
                        err = True
                elif tag == b"C":                                 # Close a statement or portal
                    kind, name = body[:1], body[1:].split(b"\x00", 1)[0].decode()
                    (statements if kind == b"S" else portals).pop(name, None)
                    conn.sendall(_msg(b"3", b""))
                elif tag == b"X":                                 # Terminate
                    break
                # H (Flush) → nothing buffered to flush
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


def _split(sql: str) -> list[str]:
    return [s for s in sql.split(";")]


def _format_of(codes: list[int], i: int) -> int:
    return 0 if not codes else codes[0] if len(codes) == 1 else codes[i]


def _param_count(sql: str, oids: list[int]) -> int:
    """How many parameters a statement takes: the declared types, else the highest $n in it."""
    import re

    found = [int(m) for m in re.findall(r"\$(\d+)", sql)]
    return max([len(oids), *found]) if found else len(oids)


def _parse_bind(body: bytes) -> tuple[str, str, list[int], list[bytes | None], list[int]]:
    """Bind → (portal, statement, parameter format codes, raw parameter values, result format codes)."""
    portal, rest = body.split(b"\x00", 1)
    stmt, rest = rest.split(b"\x00", 1)
    (nfmt,) = struct.unpack("!H", rest[:2])
    param_formats = list(struct.unpack(f"!{nfmt}h", rest[2:2 + 2 * nfmt]))
    rest = rest[2 + 2 * nfmt:]
    (nparams,) = struct.unpack("!H", rest[:2])
    rest = rest[2:]
    params: list[bytes | None] = []
    for _ in range(nparams):
        (ln,) = struct.unpack("!i", rest[:4])
        rest = rest[4:]
        if ln == -1:
            params.append(None)
        else:
            params.append(rest[:ln])
            rest = rest[ln:]
    (nres,) = struct.unpack("!H", rest[:2])
    result_formats = list(struct.unpack(f"!{nres}h", rest[2:2 + 2 * nres]))
    return portal.decode(), stmt.decode(), param_formats, params, result_formats
