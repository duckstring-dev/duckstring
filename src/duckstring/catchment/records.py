"""Records: an App line's relational data, held by the Catchment in PostgreSQL (plans/records-and-media.md).

One PostgreSQL per Catchment: one the Catchment runs itself ("managed", the default), started the first time
something needs it and kept running while the Catchment runs, or one it connects to (a ``postgres://`` URI,
credentials as ``${secret:}``/``${env:}``). Inside it, every App line ``name@major`` has a schema and a login
role of the same name, ``{name}_v{major}``, the role owning the schema and only able to write there. Every line's
role, and the one role Ponds and the viewer read with, are members of a read-only group with ``SELECT`` on every
line's schema, so Apps read each other and Ponds read everything; nothing but the line's role writes.

Passwords are never stored: each role's is an HMAC of the role name under a key kept beside the Catchment's
secrets (``records/key``, 0600), so the Catchment derives any of them when it needs to.

The PostgreSQL tools (``initdb``, ``postgres``, ``psql``, ``pg_dump``) come from the machine's PostgreSQL
(``duckstring install postgres``), so the Catchment needs no PostgreSQL client library.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets as _secrets
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlsplit

from ..app import RECORDS_NAME

log = logging.getLogger("duckstring.records")

SETTING = "records"                # "managed" (default) or a postgres:// URI
MEMORY_SETTING = "records_memory"  # bytes set aside for a managed PostgreSQL
LISTEN_SETTING = "records_listen"  # a managed PostgreSQL's listen address (default 127.0.0.1)
PORT_SETTING = "records_port"      # a managed PostgreSQL's port (chosen once, kept)
HOST_SETTING = "records_host"      # the address something on another machine reaches it at
TLS_CERT_SETTING = "records_tls_cert"  # the operator's certificate and key (paths on the Catchment's machine);
TLS_KEY_SETTING = "records_tls_key"    # without them a self-signed one is made when TLS is needed

DATABASE = "records"
DUMP = "records.dump"  # every line's Records, in catchment download and the state backup (records/ is left out)
ADMIN = "duckstring"
READER = "duckstring_reader"  # NOLOGIN: SELECT on every line's schema
PONDS = "duckstring_ponds"    # LOGIN, a READER: Ponds, the viewer and the query surface read with it
SPOUTS = "duckstring_spouts"  # LOGIN: owns the schemas Spouts deliver Pond output into (egress/records.py)
DEFAULT_MEMORY = 512 * 2**20
MAX_CONNECTIONS = 50  # the fewest a managed PostgreSQL allows; more with more memory (max_connections_for)
CONNECTION_MEMORY = 10 * 2**20  # what a connection may take, idle overhead and a query's work_mem


def max_connections_for(memory: int) -> int:
    """A managed PostgreSQL's ``max_connections`` for its memory (plans/app-scaling.md): one per
    ``CONNECTION_MEMORY``, from ``MAX_CONNECTIONS`` up to 1000."""
    return max(MAX_CONNECTIONS, min(1000, int(memory) // CONNECTION_MEMORY))
START_TIMEOUT_S = 30.0

LINE_NAME = RECORDS_NAME  # an App line's schema and role are ``{name}_v{major}``


class RecordsError(Exception):
    pass


def check_line_name(name: str) -> None:
    if not LINE_NAME.fullmatch(name):
        raise RecordsError(f"an App with Records needs a name of lower-case letters, digits and underscores, "
                           f"starting with a letter or underscore, at most 48 characters (not {name!r})")


def line_role(name: str, major: int) -> str:
    check_line_name(name)
    return f"{name}_v{int(major)}"


@dataclass(frozen=True)
class Endpoint:
    """Where the Catchment's PostgreSQL answers, and how its administrator signs in."""

    host: str
    port: int
    database: str
    admin_user: str
    admin_password: str
    sslmode: str | None = None
    managed: bool = True

    def conninfo(self, user: str | None = None, database: str | None = None) -> str:
        parts = [f"host={self.host}", f"port={self.port}", f"dbname={database or self.database}",
                 f"user={user or self.admin_user}", "connect_timeout=10"]
        if self.sslmode:
            parts.append(f"sslmode={self.sslmode}")
        return " ".join(parts)


def is_loopback(listen: str) -> bool:
    return all(part.strip() in ("127.0.0.1", "localhost", "::1") for part in listen.split(","))


def hba_lines(listen: str) -> str:
    """Who may sign in, and how: scram passwords always; beyond this machine only over TLS (``hostssl``), never
    in the clear."""
    lines = ["# Written by Duckstring at each start (catchment/records.py).",
             "local   all all scram-sha-256",
             "host    all all 127.0.0.1/32 scram-sha-256",
             "host    all all ::1/128 scram-sha-256"]
    if not is_loopback(listen):
        lines += ["hostssl all all 0.0.0.0/0 scram-sha-256", "hostssl all all ::/0 scram-sha-256"]
    return "\n".join(lines) + "\n"


def make_certificate(directory: Path, names: list[str]) -> tuple[Path, Path]:
    """A self-signed certificate and key in ``directory`` for ``names`` (host names or addresses), made with the
    ``openssl`` command; kept and reused while the names stay the same."""
    import ipaddress
    import shutil

    directory.mkdir(parents=True, exist_ok=True)
    cert, key, made_for = directory / "server.crt", directory / "server.key", directory / "names"
    wanted = ",".join(sorted(set(names)))
    if cert.is_file() and key.is_file() and made_for.is_file() and made_for.read_text() == wanted:
        return cert, key
    openssl = shutil.which("openssl")
    if openssl is None:
        raise RecordsError("Records over TLS needs a certificate: install openssl (so one is made), or set "
                           "`duckstring catchment records --tls-cert PATH --tls-key PATH`")
    alt = []
    for n in sorted(set(names)):
        try:
            ipaddress.ip_address(n)
            alt.append(f"IP:{n}")
        except ValueError:
            alt.append(f"DNS:{n}")
    out = subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650", "-keyout", str(key),
         "-out", str(cert), "-subj", f"/CN={sorted(set(names))[0]}", "-addext", "subjectAltName=" + ",".join(alt)],
        capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RecordsError(f"making a certificate for Records failed: {out.stderr.strip()[-1000:]}")
    key.chmod(0o600)  # PostgreSQL refuses a key others can read
    made_for.write_text(wanted)
    return cert, key


def certificate_fingerprint(cert: Path) -> str | None:
    """The certificate's SHA-256 fingerprint, for a client to check a self-signed one."""
    import ssl

    try:
        pem = cert.read_text()
        der = ssl.PEM_cert_to_DER_cert(pem)
    except (OSError, ValueError):
        return None
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def parse_external(uri: str) -> Endpoint:
    """An external PostgreSQL's endpoint from its URI, credentials resolved (``${secret:}``/``${env:}``)."""
    from ..egress.credentials import resolve

    parts = urlsplit(uri)
    if parts.scheme not in ("postgres", "postgresql"):
        raise RecordsError(f"Records needs a postgres:// URI, not {parts.scheme or uri!r}")
    user = resolve(unquote(parts.username or ""))
    password = resolve(unquote(parts.password or ""))
    if not parts.hostname or not user:
        raise RecordsError("the Records URI needs a host and a user (postgres://user:password@host/database)")
    query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
    database = unquote(parts.path.lstrip("/")) or "postgres"
    return Endpoint(parts.hostname, parts.port or 5432, database, user, password,
                    sslmode=query.get("sslmode"), managed=False)


class ManagedPostgres:
    """A PostgreSQL cluster in ``{root}/records/pg``, run as a child of the Catchment."""

    def __init__(self, base: Path, data_name: str = "pg", major: int | None = None):
        self.base = Path(base)
        self.data = self.base / data_name
        self.log_path = self.base / ("postgres.log" if data_name == "pg" else f"postgres.{data_name}.log")
        self._started_path = self.base / ("started.json" if data_name == "pg" else f"started.{data_name}.json")
        self._want_major = major  # a new cluster's PostgreSQL major (a move to another version)
        self._proc: subprocess.Popen | None = None
        self._adopted: int | None = None  # a postmaster a previous Catchment process left running
        self.bin_dir: Path | None = None
        self.major: int | None = None

    def _bin(self) -> Path:
        from ..install import find_postgres

        wanted = self._want_major
        version_file = self.data / "PG_VERSION"
        if version_file.is_file():
            wanted = int(version_file.read_text().strip().split(".")[0])
        found = find_postgres(wanted)
        if found is None:
            which = f"PostgreSQL {wanted}" if wanted else "PostgreSQL"
            raise RecordsError(f"Records needs {which} on this machine: run `duckstring install postgres"
                               + (f" --postgres-version {wanted}" if wanted else "") + "`")
        self.bin_dir, self.major = found
        return self.bin_dir

    def initialised(self) -> bool:
        return (self.data / "PG_VERSION").is_file()

    def initdb(self, admin_password: str) -> None:
        bin_dir = self._bin()
        self.base.mkdir(parents=True, exist_ok=True)
        pwfile = self.base / ".initdb-password"
        pwfile.write_text(admin_password)
        pwfile.chmod(0o600)
        locale = (["--locale-provider=builtin", "--builtin-locale=C.UTF-8"] if (self.major or 0) >= 17
                  else ["--no-locale"])
        try:
            out = subprocess.run(
                [str(bin_dir / "initdb"), "-D", str(self.data), "-U", ADMIN, f"--pwfile={pwfile}",
                 "--auth-local=scram-sha-256", "--auth-host=scram-sha-256", "-E", "UTF8", *locale],
                capture_output=True, text=True, timeout=120)
        finally:
            pwfile.unlink(missing_ok=True)
        if out.returncode != 0:
            raise RecordsError(f"initdb failed: {(out.stderr or out.stdout).strip()[-2000:]}")

    def _write_hba(self, listen: str) -> None:
        (self.data / "pg_hba.conf").write_text(hba_lines(listen))

    def _postmaster(self) -> tuple[int, int] | None:
        """The pid and port a running postmaster recorded for this cluster, when it is still alive."""
        try:
            lines = (self.data / "postmaster.pid").read_text().splitlines()
            pid, port = int(lines[0]), int(lines[3])
        except (OSError, ValueError, IndexError):
            return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            pass
        return pid, port

    def running(self) -> bool:
        if self._proc is not None:
            return self._proc.poll() is None
        if self._adopted is not None:
            try:
                os.kill(self._adopted, 0)
                return True
            except ProcessLookupError:
                self._adopted = None
        return False

    def start(self, port: int, listen: str, memory: int, tls: tuple[Path, Path] | None = None) -> None:
        bin_dir = self._bin()
        # What it is started with: a running postmaster is adopted only when it was started the same way.
        started = json.dumps({"port": port, "listen": listen, "memory": memory,
                              "tls": [str(p) for p in tls] if tls else None}, sort_keys=True)
        existing = self._postmaster()
        if existing is not None:
            recorded = self._started_path
            if existing[1] == port and recorded.is_file() and recorded.read_text() == started:
                self._adopted = existing[0]
                log.info("records: adopted the running PostgreSQL (pid %s)", existing[0])
                return
            self._signal_stop(existing[0])
        if not is_loopback(listen) and tls is None:
            raise RecordsError("a PostgreSQL listening beyond this machine needs TLS")
        self._write_hba(listen)
        kb = lambda n: f"{max(n, 0) // 1024}kB"  # noqa: E731
        settings = {
            "port": str(port), "listen_addresses": listen, "unix_socket_directories": "",
            "max_connections": str(max_connections_for(memory)),
            "shared_buffers": kb(max(16 * 2**20, memory // 4)),
            "work_mem": "4MB", "maintenance_work_mem": kb(max(16 * 2**20, memory // 16)),
            "effective_cache_size": kb(memory),
            "logging_collector": "off",
        }
        if tls is not None:
            settings.update({"ssl": "on", "ssl_cert_file": str(tls[0]), "ssl_key_file": str(tls[1]),
                             "ssl_min_protocol_version": "TLSv1.2"})
        argv = [str(bin_dir / "postgres"), "-D", str(self.data)]
        for k, v in settings.items():
            argv += ["-c", f"{k}={v}"]
        self.base.mkdir(parents=True, exist_ok=True)
        log_file = open(self.log_path, "ab")
        try:
            self._proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
                                          start_new_session=True)
        finally:
            log_file.close()
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RecordsError(f"PostgreSQL exited at start: {self.log_tail()}")
            ready = subprocess.run([str(bin_dir / "pg_isready"), "-q", "-h", "127.0.0.1", "-p", str(port)],
                                   capture_output=True, timeout=10)
            if ready.returncode == 0:
                self._started_path.write_text(started)
                return
            time.sleep(0.1)
        self.stop()
        raise RecordsError(f"PostgreSQL didn't start within {START_TIMEOUT_S:.0f} s: {self.log_tail()}")

    def log_tail(self, lines: int = 20) -> str:
        try:
            return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return ""

    @staticmethod
    def _signal_stop(pid: int, timeout: float = 30.0) -> None:
        try:
            os.kill(pid, signal.SIGINT)  # PostgreSQL's fast shutdown
        except ProcessLookupError:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        os.kill(pid, signal.SIGKILL)

    def stop(self) -> None:
        if self._proc is not None:
            # Its own child: wait on it, since an exited child the Catchment hasn't reaped still answers kill(0).
            if self._proc.poll() is None:
                self._proc.send_signal(signal.SIGINT)  # PostgreSQL's fast shutdown
                try:
                    self._proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait()
            self._proc = None
        if self._adopted is not None:
            self._signal_stop(self._adopted)
            self._adopted = None


class Records:
    """The Catchment's Records: settings, the managed PostgreSQL's life, and each App line's schema and role."""

    def __init__(self, root: Path, db, reserve: Callable[[int], None] | None = None,
                 release: Callable[[], None] | None = None):
        self.root = Path(root)
        self.db = db
        self.base = self.root / "records"
        self.pg = ManagedPostgres(self.base)
        self._reserve = reserve
        self._release = release
        self._lock = threading.RLock()
        self._ready: Endpoint | None = None
        self._provisioned: set[str] = set()
        self._external_max: int | None = None  # an external server's max_connections, once asked
        self._limits: dict[str, int] = {}  # line role -> the CONNECTION LIMIT set on it
        self._stopping = False  # the Catchment is shutting down: keep_alive starts nothing
        self.held = False  # a move is under way: keep_alive starts nothing (catchment/moves.py)
        self._last_warning = 0.0

    # ── settings ───────────────────────────────────────────────────────────────────────────────────────

    def _setting(self, key: str) -> str | None:
        from .cloud import get_setting
        from .db import locked

        with locked(self.db):
            return get_setting(self.db, key)

    def _set(self, key: str, value: str | None) -> None:
        from .cloud import set_setting
        from .db import locked

        with locked(self.db):
            set_setting(self.db, key, value)

    @property
    def target(self) -> str:
        return self._setting(SETTING) or "managed"

    @property
    def managed(self) -> bool:
        return self.target == "managed"

    @property
    def memory(self) -> int:
        value = self._setting(MEMORY_SETTING)
        return int(value) if value else DEFAULT_MEMORY

    @property
    def listen(self) -> str:
        return self._setting(LISTEN_SETTING) or "127.0.0.1"

    @property
    def host(self) -> str:
        """The address something on another machine reaches a managed PostgreSQL at: ``records_host``, else a
        specific listen address, else this machine's name."""
        set_host = self._setting(HOST_SETTING)
        if set_host:
            return set_host
        first = self.listen.split(",")[0].strip()
        return first if first not in ("*", "0.0.0.0", "::") else socket.gethostname()

    def _operator_certificate(self) -> bool:
        return bool((self._setting(TLS_CERT_SETTING) or os.environ.get("DUCKSTRING_RECORDS_TLS_CERT"))
                    and (self._setting(TLS_KEY_SETTING) or os.environ.get("DUCKSTRING_RECORDS_TLS_KEY")))

    def max_connections(self) -> int:
        """The connections the PostgreSQL allows: a managed one's from its memory; an external one's as it says
        (asked once)."""
        if self.managed:
            return max_connections_for(self.memory)
        if self._external_max is None:
            self._external_max = int(self.psql("SHOW max_connections").strip())
        return self._external_max

    def set_connection_limit(self, name: str, major: int, limit: int) -> None:
        """Cap the connections an App line's role may hold at once: its instances' budget (plans/app-scaling.md),
        so one App's pools can't take every connection."""
        role = line_role(name, major)
        # Under the lock provision() holds: two sessions altering one role at once fail with "tuple concurrently
        # updated" (an instance starting while the supervisor set its line's limit).
        with self._lock:
            if self._limits.get(role) == limit:
                return
            self.psql(f'ALTER ROLE "{role}" CONNECTION LIMIT {int(limit)}')
            self._limits[role] = limit

    def remote_ca(self) -> str | None:
        """The certificate something elsewhere trusts to verify the managed PostgreSQL (PEM): the self-signed one
        made for its address. ``None`` for an operator's certificate (verified against the system's roots) and for
        an external server (its URL says how)."""
        if not self.managed or self._operator_certificate():
            return None
        pair = self.tls()
        return pair[0].read_text() if pair else None

    def tls(self) -> tuple[Path, Path] | None:
        """The certificate and key a managed PostgreSQL serves TLS with: the operator's, else a self-signed one
        made for its address. None while it listens on this machine only (nothing reaches it from elsewhere)."""
        cert = self._setting(TLS_CERT_SETTING) or os.environ.get("DUCKSTRING_RECORDS_TLS_CERT")
        key = self._setting(TLS_KEY_SETTING) or os.environ.get("DUCKSTRING_RECORDS_TLS_KEY")
        if cert and key:
            return Path(cert).expanduser(), Path(key).expanduser()
        if is_loopback(self.listen):
            return None
        names = [self.host, socket.gethostname(), "localhost", "127.0.0.1"]
        names += [a.strip() for a in self.listen.split(",") if a.strip() not in ("*", "0.0.0.0", "::")]
        return make_certificate(self.base / "tls", names)

    def in_use(self) -> bool:
        """Whether any App line has Records, or a Spout delivers into them: the target can't change after that
        without a move."""
        from .db import locked

        with locked(self.db):
            if self.db.execute("SELECT 1 FROM app_data WHERE records = 1 LIMIT 1").fetchone() is not None:
                return True
            return self.db.execute(
                "SELECT 1 FROM pond_spout WHERE destination LIKE 'records:%' LIMIT 1").fetchone() is not None

    def configure(self, target: str | None = None, memory: int | None = None, listen: str | None = None,
                  host: str | None = None, tls_cert: str | None = None, tls_key: str | None = None) -> None:
        """Change the settings. The target can't change once a line has Records (a move copies them across);
        the rest apply at the managed PostgreSQL's next start, which a running one is given now."""
        restart = False
        if target is not None:
            target = target.strip() or "managed"
            if target != "managed":
                parse_external(target)  # checks the URI and its references now
            if target != self.target:
                if self.in_use():
                    raise RecordsError("App lines already hold Records here; the Records PostgreSQL can't change "
                                       "now (a move that copies them across is the way)")
                self.stop()
                self._set(SETTING, None if target == "managed" else target)
        if memory is not None:
            if memory < 64 * 2**20:
                raise RecordsError("Records needs at least 64 MB")
            self._set(MEMORY_SETTING, str(int(memory)))
        if listen is not None:
            self._set(LISTEN_SETTING, listen.strip() or None)
            restart = True
        if host is not None:
            self._set(HOST_SETTING, host.strip() or None)
            restart = True
        if (tls_cert is None) != (tls_key is None):
            raise RecordsError("give the certificate and its key together")
        if tls_cert is not None:
            for path in (tls_cert, tls_key):
                if path.strip() and not Path(path).expanduser().is_file():
                    raise RecordsError(f"no file {path!r} on the Catchment's machine")
            self._set(TLS_CERT_SETTING, tls_cert.strip() or None)
            self._set(TLS_KEY_SETTING, tls_key.strip() or None)
            restart = True
        if memory is not None:
            restart = True
        if restart and self.managed and self.pg.running():
            self.stop()  # started again, with the new settings, by the next need (or keep_alive)

    # ── keys and passwords ─────────────────────────────────────────────────────────────────────────────

    def _key(self) -> bytes:
        path = self.base / "key"
        try:
            return bytes.fromhex(path.read_text().strip())
        except FileNotFoundError:
            pass
        self.base.mkdir(parents=True, exist_ok=True)
        key = _secrets.token_bytes(32)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(key.hex())
        tmp.chmod(0o600)
        os.replace(tmp, path)
        return key

    def password(self, role: str) -> str:
        return hmac.new(self._key(), role.encode(), hashlib.sha256).hexdigest()[:40]

    # ── the server ─────────────────────────────────────────────────────────────────────────────────────

    def ensure(self) -> Endpoint:
        """The PostgreSQL ready for use: a managed one initialised and started (its memory set aside), the
        database and the shared roles in place. Idempotent and cheap once ready."""
        with self._lock:
            if self._ready is not None and (not self._ready.managed or self.pg.running()):
                return self._ready
            if self._stopping:
                raise RecordsError("the Catchment is shutting down")
            self._ready = None
            self._provisioned.clear()
            self._limits.clear()
            self._external_max = None
            if self.managed:
                fresh = not self.pg.initialised()
                endpoint = self._start_managed()
            else:
                fresh, endpoint = False, parse_external(self.target)
            self._bootstrap(endpoint)
            if fresh:
                self._restore_dump(endpoint)
            self._ready = endpoint
            return endpoint

    def _start_managed(self) -> Endpoint:
        admin_password = self.password(ADMIN)
        if not self.pg.initialised():
            self.pg.initdb(admin_password)
        port_value = self._setting(PORT_SETTING)
        port = int(port_value) if port_value else _free_port()
        if not port_value:
            self._set(PORT_SETTING, str(port))
        memory = self.memory
        if self._reserve is not None:
            try:
                self._reserve(memory)
            except ValueError as e:
                raise RecordsError(str(e)) from None
        try:
            tls = self.tls()
            try:
                self.pg.start(port, self.listen, memory, tls)
            except RecordsError:
                if "could not bind" not in self.pg.log_tail(5) and "in use" not in self.pg.log_tail(5):
                    raise
                port = _free_port()  # something else took the port: choose another, keep it
                self._set(PORT_SETTING, str(port))
                self.pg.start(port, self.listen, memory, tls)
        except BaseException:
            if self._release is not None:
                self._release()
            raise
        return Endpoint("127.0.0.1", port, DATABASE, ADMIN, admin_password, managed=True)

    def shutdown(self) -> None:
        """The Catchment is stopping: stop a managed PostgreSQL and start nothing after."""
        self._stopping = True
        self.stop()

    def stop(self) -> None:
        """Stop a managed PostgreSQL, giving its memory back."""
        with self._lock:
            was = self.pg.running()
            self.pg.stop()
            self._ready = None
            self._provisioned.clear()
            self._limits.clear()
            self._external_max = None
            if was and self._release is not None:
                self._release()

    # ── SQL ────────────────────────────────────────────────────────────────────────────────────────────

    def _tool(self, name: str) -> str:
        if self.pg.bin_dir is None:
            self.pg._bin()
        return str(self.pg.bin_dir / name)

    def psql(self, sql: str, endpoint: Endpoint | None = None, database: str | None = None) -> str:
        """Run ``sql`` as the administrator; the unaligned, tuples-only output."""
        endpoint = endpoint or self.ensure()
        env = {**os.environ, "PGPASSWORD": endpoint.admin_password}
        out = subprocess.run(
            [self._tool("psql"), "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
             "-d", endpoint.conninfo(database=database), "-c", sql],
            capture_output=True, text=True, env=env, timeout=120)
        if out.returncode != 0:
            raise RecordsError(f"PostgreSQL: {(out.stderr or out.stdout).strip()[-2000:]}")
        return out.stdout

    def _bootstrap(self, endpoint: Endpoint) -> None:
        if endpoint.managed:
            exists = self.psql(f"SELECT 1 FROM pg_database WHERE datname = {_sql_literal(DATABASE)}",
                               endpoint, database="postgres").strip()
            if not exists:
                self.psql(f'CREATE DATABASE "{DATABASE}"', endpoint, database="postgres")
                self.psql("REVOKE CREATE ON SCHEMA public FROM PUBLIC", endpoint)
        ponds = _sql_literal(self.password(PONDS))
        self.psql(f"""
            DO $$BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{READER}') THEN
                CREATE ROLE "{READER}" NOLOGIN;
              END IF;
              IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{PONDS}') THEN
                CREATE ROLE "{PONDS}" LOGIN PASSWORD {ponds};
              ELSE
                ALTER ROLE "{PONDS}" LOGIN PASSWORD {ponds};
              END IF;
            END$$;
            GRANT "{READER}" TO "{PONDS}";
        """, endpoint)

    # ── App lines ──────────────────────────────────────────────────────────────────────────────────────

    def provision(self, name: str, major: int) -> None:
        """The line's schema and role, made or brought up to date (idempotent): the role owns the schema,
        signs in with its derived password, searches its schema first, and reads every line's schema."""
        role = line_role(name, major)
        with self._lock:
            endpoint = self.ensure()
            if role in self._provisioned:
                return
            pw = _sql_literal(self.password(role))
            self.psql(f"""
                DO $$BEGIN
                  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    CREATE ROLE "{role}" LOGIN PASSWORD {pw};
                  ELSE
                    ALTER ROLE "{role}" LOGIN PASSWORD {pw};
                  END IF;
                END$$;
                GRANT "{READER}" TO "{role}";
                GRANT "{role}" TO CURRENT_USER;
                CREATE SCHEMA IF NOT EXISTS "{role}" AUTHORIZATION "{role}";
                GRANT USAGE ON SCHEMA "{role}" TO "{READER}";
                GRANT SELECT ON ALL TABLES IN SCHEMA "{role}" TO "{READER}";
                ALTER DEFAULT PRIVILEGES FOR ROLE "{role}" IN SCHEMA "{role}" GRANT SELECT ON TABLES TO "{READER}";
                ALTER ROLE "{role}" SET search_path = "{role}";
                GRANT CONNECT, TEMPORARY ON DATABASE "{endpoint.database}" TO "{role}", "{PONDS}";
            """, endpoint)
            self._provisioned.add(role)

    def spout_url(self, schema: str) -> str:
        """Where a Spout delivers into Records: the ``duckstring_spouts`` role, after making ``schema`` (owned by
        it, readable by every App line). Idempotent; starts PostgreSQL if need be."""
        if not re.fullmatch(r"[a-z_][a-z0-9_]*_v[0-9]+", schema):
            raise RecordsError(f"not a Spout schema: {schema!r}")
        with self._lock:
            endpoint = self.ensure()
            if schema not in self._provisioned:
                pw = _sql_literal(self.password(SPOUTS))
                self.psql(f"""
                    DO $$BEGIN
                      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{SPOUTS}') THEN
                        CREATE ROLE "{SPOUTS}" LOGIN PASSWORD {pw};
                      ELSE
                        ALTER ROLE "{SPOUTS}" LOGIN PASSWORD {pw};
                      END IF;
                    END$$;
                    GRANT "{SPOUTS}" TO CURRENT_USER;
                    CREATE SCHEMA IF NOT EXISTS "{schema}" AUTHORIZATION "{SPOUTS}";
                    GRANT USAGE ON SCHEMA "{schema}" TO "{READER}";
                    GRANT SELECT ON ALL TABLES IN SCHEMA "{schema}" TO "{READER}";
                    ALTER DEFAULT PRIVILEGES FOR ROLE "{SPOUTS}" IN SCHEMA "{schema}" GRANT SELECT ON TABLES TO "{READER}";
                    GRANT CONNECT, TEMPORARY ON DATABASE "{endpoint.database}" TO "{SPOUTS}";""", endpoint)
                self._provisioned.add(schema)
        return self.url(SPOUTS)

    def freeze(self, name: str, major: int) -> None:
        """Make a line's Records read-only: its role can no longer sign in, and its sessions end."""
        role = line_role(name, major)
        with self._lock:
            self._provisioned.discard(role)
            self.psql(f"""ALTER ROLE "{role}" NOLOGIN;
                SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{role}';""")

    def drop(self, name: str, major: int) -> None:
        """Delete a line's Records: its schema with everything in it, and its role."""
        role = line_role(name, major)
        with self._lock:
            self._provisioned.discard(role)
            self.psql(f"""
                SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{role}';
                DROP SCHEMA IF EXISTS "{role}" CASCADE;
                DO $$BEGIN
                  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    REASSIGN OWNED BY "{role}" TO CURRENT_USER;
                    DROP OWNED BY "{role}";
                    DROP ROLE "{role}";
                  END IF;
                END$$;""")

    def _known_endpoint(self) -> Endpoint | None:
        """Where PostgreSQL answers, from the settings alone (nothing started); None before a managed one has a
        port."""
        if self._ready is not None:
            return self._ready
        if not self.managed:
            return parse_external(self.target)
        port = self._setting(PORT_SETTING)
        if not port:
            return None
        return Endpoint("127.0.0.1", int(port), DATABASE, ADMIN, self.password(ADMIN), managed=True)

    def keep_alive(self) -> None:
        """Start a managed PostgreSQL again if App lines hold Records and it isn't running (the Catchment's
        restart, or a crash), so Ponds and Apps find it. Called from the App supervisor's loop."""
        if self.held:  # a move is switching clusters: it starts what it needs itself
            return
        if self.managed and not self._stopping and not self.pg.running() and self.in_use():
            try:
                self.ensure()
            except RecordsError as e:
                now = time.monotonic()
                if now - self._last_warning > 300:
                    self._last_warning = now
                    log.warning("records: PostgreSQL isn't running and couldn't start: %s", e)

    def _run_tool(self, argv: list[str], endpoint: Endpoint, what: str) -> None:
        env = {**os.environ, "PGPASSWORD": endpoint.admin_password}
        out = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=3600)
        if out.returncode != 0:
            raise RecordsError(f"{what}: {(out.stderr or out.stdout).strip()[-2000:]}")

    def copy_line(self, src: tuple[str, int], dst: tuple[str, int]) -> None:
        """Replace the line ``dst``'s Records with a copy of ``src``'s: every table, view, sequence, index and
        function, owned by ``dst``'s role, ``src``'s left as it was (so it can be copied again, and keeps serving
        meanwhile). The schema is renamed in a scratch database, since a dump can't be restored under another
        schema's name."""
        a, b = line_role(*src), line_role(*dst)
        with self._lock:
            endpoint = self.ensure()
            self.provision(*dst)
            scratch = f"duckstring_copy_{_secrets.token_hex(4)}"
            work = self.base / "copies"
            work.mkdir(parents=True, exist_ok=True)
            first, second = work / f"{scratch}.src.dump", work / f"{scratch}.dst.dump"
            pg_dump, pg_restore = self._tool("pg_dump"), self._tool("pg_restore")
            self.psql(f'CREATE DATABASE "{scratch}"', endpoint, database="postgres")
            try:
                self._run_tool([pg_dump, "-Fc", "-n", a, "-f", str(first), "-d", endpoint.conninfo()], endpoint,
                               f"copying {src[0]}@{src[1]}'s Records")
                self._run_tool([pg_restore, "-d", endpoint.conninfo(database=scratch), str(first)], endpoint,
                               "restoring the copy")
                self.psql(f'''ALTER SCHEMA "{a}" RENAME TO "{b}";
                    REASSIGN OWNED BY "{a}" TO "{b}";
                    ALTER DEFAULT PRIVILEGES FOR ROLE "{a}" IN SCHEMA "{b}" REVOKE SELECT ON TABLES FROM "{READER}";''',
                          endpoint, database=scratch)
                self._run_tool([pg_dump, "-Fc", "-n", b, "-f", str(second), "-d", endpoint.conninfo(database=scratch)],
                               endpoint, "renaming the copy")
                self.psql(f'''SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{b}';
                    DROP SCHEMA IF EXISTS "{b}" CASCADE;''', endpoint)
                self._run_tool([pg_restore, "-d", endpoint.conninfo(), str(second)], endpoint,
                               f"installing the copy as {dst[0]}@{dst[1]}'s Records")
            finally:
                first.unlink(missing_ok=True)
                second.unlink(missing_ok=True)
                self.psql(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)', endpoint, database="postgres")
            self._provisioned.discard(b)
            self.provision(*dst)  # grants, search path, and its sign-in, over the restored schema

    def dump(self, out: Path) -> bool:
        """Every line's Records as one ``pg_dump`` archive at ``out`` (for ``catchment download`` and the state
        backup, which leave the cluster's own files out: they can't be copied while it runs). False when no line
        holds Records, or they live in an external PostgreSQL (whose own backups cover them)."""
        if not self.managed or not self.in_use():
            return False
        endpoint = self.ensure()
        self._run_tool([self._tool("pg_dump"), "-Fc", "-f", str(out), "-d", endpoint.conninfo()], endpoint,
                       "dumping Records")
        return True

    def _restore_dump(self, endpoint: Endpoint) -> None:
        """A fresh managed cluster beside a ``records.dump`` (a restored download or state backup): make every
        line's role, then restore the dump into the database, which recreates the schemas with their owners."""
        dump = self.base / DUMP
        if not dump.is_file():
            return
        from .db import locked

        with locked(self.db):
            lines = self.db.execute("SELECT name, major FROM app_data WHERE records = 1").fetchall()
        for name, major in lines:
            role = line_role(name, major)
            pw = _sql_literal(self.password(role))
            self.psql(f"""
                DO $$BEGIN
                  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    CREATE ROLE "{role}" LOGIN PASSWORD {pw};
                  END IF;
                END$$;
                GRANT "{READER}" TO "{role}";""", endpoint)
        self._run_tool([self._tool("pg_restore"), "--exit-on-error", "-d", endpoint.conninfo(), str(dump)], endpoint,
                       "restoring Records from records.dump")
        dump.rename(dump.with_name(f"{DUMP}.restored-{time.strftime('%Y%m%dT%H%M%S')}"))
        log.info("records: restored %d line(s) from %s", len(lines), DUMP)

    def url(self, role: str, host: str | None = None, start: bool = True, remote: bool = False,
            verify: bool = False) -> str:
        """A standard libpq URL signing ``role`` in (its search path is set on the role itself). With
        ``start=False`` it is built from the settings, never starting anything. ``remote``: for something on
        another machine, at the managed PostgreSQL's reachable address and over TLS (``sslmode=require``);
        ``verify`` checks the server too (``verify-full``: against :meth:`remote_ca`, which the caller adds as
        ``sslrootcert``, or for an operator's certificate the system's roots)."""
        endpoint = (self._known_endpoint() if not start else None) or self.ensure()
        sslmode = endpoint.sslmode
        extra = ""
        if remote and endpoint.managed:
            if is_loopback(self.listen):
                raise RecordsError("Records' PostgreSQL listens on this machine only: set `duckstring catchment "
                                   "records --listen ADDRESS` for something elsewhere to reach it")
            host, sslmode = host or self.host, ("verify-full" if verify else "require")
            if verify and self._operator_certificate():  # else the caller trusts remote_ca()
                extra = "&sslrootcert=system"
        query = f"?sslmode={sslmode}{extra}" if sslmode else ""
        return (f"postgresql://{role}:{self.password(role)}@{host or endpoint.host}:{endpoint.port}/"
                f"{endpoint.database}{query}")

    def line_url(self, name: str, major: int, host: str | None = None) -> str:
        return self.url(line_role(name, major), host)

    def reader_url(self, host: str | None = None, start: bool = True, remote: bool = False,
                   verify: bool = False) -> str:
        return self.url(PONDS, host, start=start, remote=remote, verify=verify)

    def tables(self, schemas: list[str]) -> dict[str, list[dict]]:
        """Each schema's tables and views, ``{schema: [{table, kind, rows}]}``; ``rows`` is PostgreSQL's estimate
        (``reltuples``, -1 before a table is first analysed, shown as unknown)."""
        if not schemas:
            return {}
        names = ", ".join(_sql_literal(s) for s in schemas)
        out = self.psql(f"""
            SELECT n.nspname, c.relname, c.relkind, c.reltuples::bigint FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname IN ({names}) AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
            ORDER BY n.nspname, c.relname""")
        found: dict[str, list[dict]] = {s: [] for s in schemas}
        kinds = {"r": "table", "p": "table", "v": "view", "m": "view", "f": "table"}
        for line in out.splitlines():
            if not line.strip():
                continue
            schema, table, kind, rows = line.split("|")
            found.setdefault(schema, []).append({"table": table, "kind": kinds.get(kind, "table"),
                                                 "rows": int(rows) if int(rows) >= 0 else None})
        return found

    def read_parquet(self, schema: str, table: str, out: Path) -> None:
        """One line's Records table written to ``out`` as Parquet, read as the reader role (for a Duck on another
        machine, through the Catchment's Duck channel)."""
        import duckdb

        if not re.fullmatch(r"[a-z_][a-z0-9_]*_v[0-9]+", schema):
            raise RecordsError(f"not a Records schema: {schema!r}")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", table):
            raise RecordsError(f"not a Records table: {table!r}")
        con = duckdb.connect()
        try:
            con.execute("INSTALL postgres; LOAD postgres")
            con.execute(f"ATTACH '{self.reader_url()}' AS r (TYPE postgres, READ_ONLY)")
            con.execute(f"""COPY (SELECT * FROM r."{schema}"."{table}") TO '{out}' (FORMAT PARQUET)""")
        except duckdb.CatalogException as e:
            raise RecordsError(f"no Records table {schema}.{table}") from e
        finally:
            con.close()

    def status(self) -> dict:
        out = {"target": "managed" if self.managed else "external", "running": False,
               "memory": self.memory if self.managed else None, "listen": self.listen if self.managed else None}
        if self.managed:
            out["running"] = self.pg.running()
            out["host"] = self.host
            cert = self._setting(TLS_CERT_SETTING) or os.environ.get("DUCKSTRING_RECORDS_TLS_CERT")
            made = self.base / "tls" / "server.crt"
            tls_on = not is_loopback(self.listen) or bool(cert)
            out["tls"] = {"on": tls_on, "certificate": "operator" if cert else ("self-signed" if tls_on else None),
                          "fingerprint": certificate_fingerprint(Path(cert) if cert else made) if tls_on else None}
            port = self._setting(PORT_SETTING)
            out["port"] = int(port) if port else None
            out["initialised"] = self.pg.initialised()
            if self.pg.initialised():
                out["postgres_major"] = int((self.pg.data / "PG_VERSION").read_text().strip().split(".")[0])
        else:
            endpoint = parse_external(self.target)
            out.update(host=endpoint.host, port=endpoint.port, database=endpoint.database,
                       running=self._ready is not None)
        return out
