"""Moving Records or Media to another place, and a managed PostgreSQL to another major (plans/records-and-media.md).

Where Records and Media live is fixed once a line holds them; a move is how they change. One move at a time, run in
the background (``status()`` reports its progress), refused while a promotion's cutover runs.

**Records** (``catchment records move managed|postgres://... [--postgres-version N]``):

1. Check the target: reachable, its credential can make roles and databases, and none of this Catchment's schemas
   or roles there. A managed target is a new cluster (``records/pg.new``) of the requested major.
2. The window: Apps with Records answer 503 and stop, ``records:`` Spouts wait, and every role that writes is
   frozen (sessions ended), so nothing changes while the copy is taken.
3. Copy: the whole database as one ``pg_dump``, the roles made on the target with their derived passwords, the dump
   restored.
4. Verify: every table's row count matches.
5. Switch: the setting (or, for a managed cluster, the new data directory in place of the old), lines signed in
   again (a retired one stays frozen).
6. Delete the old store: a managed cluster's directory, or this Catchment's schemas and roles in an external one.

A failure before the switch removes what the move made on the target and signs the lines in again: the old store
is as it was.

**Media** (``catchment media move local|s3://...``): every file copied while Apps run; then the window (Apps with
Media stop) copies what changed since and removes what was deleted; the listings are compared, the setting
switched, and the old files deleted.
"""

from __future__ import annotations

import logging
import secrets as _secrets
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("duckstring.moves")


class MoveError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _Chunks:
    """A readable stream over an iterator of byte chunks (Media.read → Media.write)."""

    def __init__(self, chunks):
        self._it = iter(chunks)

    def read(self, _n: int = -1) -> bytes:
        return next(self._it, b"")


class Moves:
    def __init__(self, driver):
        self.driver = driver
        self._moving: set[str] = set()  # "records" | "media" while its window is open
        self._mu = threading.Lock()
        self._thread: threading.Thread | None = None
        self._status: dict = {"kind": None, "state": "idle"}

    # ── what the rest of the Catchment asks ────────────────────────────────────────────────────────────

    def moving(self, kind: str) -> bool:
        return kind in self._moving

    def blocks(self, cfg: dict | None) -> bool:
        """Whether an App with this ``[app]`` must wait: its Records or Media are being moved."""
        if not cfg:
            return False
        return (bool(cfg.get("records")) and "records" in self._moving) or (
            bool(cfg.get("media")) and "media" in self._moving)

    def status(self) -> dict:
        with self._mu:
            return dict(self._status)

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _set(self, **kw) -> None:
        with self._mu:
            self._status.update(kw)

    # ── starting one ───────────────────────────────────────────────────────────────────────────────────

    def start(self, kind: str, target: str, postgres_version: int | None = None) -> dict:
        """Check the request and run the move in the background. Raises :class:`MoveError`."""
        if self.busy():
            raise MoveError(f"a move of {self._status.get('kind')} is already running")
        lifecycle = getattr(self.driver, "lifecycle", None)
        if lifecycle is not None and lifecycle._cutovers:
            raise MoveError("an App is being promoted; move once it has finished")
        target = (target or "").strip()
        if kind == "records":
            self._check_records_request(target, postgres_version)
            run = lambda: self._move_records(target, postgres_version)  # noqa: E731
        elif kind == "media":
            self._check_media_request(target)
            run = lambda: self._move_media(target)  # noqa: E731
        else:
            raise MoveError(f"nothing called {kind!r} moves")
        with self._mu:
            self._status = {"kind": kind, "target": target, "postgres_version": postgres_version, "state": "starting",
                            "detail": "", "error": None, "started_at": _now(), "finished_at": None}

        def go() -> None:
            try:
                run()
                self._set(state="done", detail="", finished_at=_now())
            except Exception as e:  # reported on status; the old store is as it was unless it got past the switch
                log.exception("moving %s", kind)
                self._set(state="failed", error=str(e), finished_at=_now())
            finally:
                self._moving.discard(kind)
                self.driver._signal_egress()

        self._thread = threading.Thread(target=go, name=f"duckstring-move-{kind}", daemon=True)
        self._thread.start()
        return self.status()

    def wait(self, timeout: float | None = None) -> dict:
        if self._thread is not None:
            self._thread.join(timeout)
        return self.status()

    # ── Records ────────────────────────────────────────────────────────────────────────────────────────

    def _check_records_request(self, target: str, version: int | None) -> None:
        from .records import parse_external

        records = self.driver.records
        if target not in ("managed",):
            try:
                parse_external(target)
            except Exception as e:
                raise MoveError(str(e)) from None
            if version is not None:
                raise MoveError("--postgres-version is for a managed PostgreSQL")
        current_major = None
        if records.managed and records.pg.initialised():
            current_major = int((records.pg.data / "PG_VERSION").read_text().strip().split(".")[0])
        if target == records.target and (target != "managed" or version is None or version == current_major):
            raise MoveError("Records are already there")
        if target == "managed" and version is not None:
            from ..install import find_postgres

            if find_postgres(version) is None:
                raise MoveError(f"PostgreSQL {version} isn't on this machine: duckstring install postgres "
                                f"--postgres-version {version}")

    def _schemas(self) -> tuple[list[tuple[str, int, str]], list[str]]:
        """The App lines with Records (name, major, state) and the Spouts' schemas."""
        from .db import locked

        lines = [(d["name"], d["major"], d["state"]) for d in self.driver.app_data() if d["records"]]
        with locked(self.driver.db):
            spouts = sorted({r[0][len("records:"):] for r in self.driver.db.execute(
                "SELECT destination FROM pond_spout WHERE destination LIKE 'records:%'").fetchall()})
        return lines, spouts

    def _stop_apps(self, which: str) -> None:
        stop = getattr(self.driver, "on_app_stop", None)
        for key in self.driver.app_keys():
            view = self.driver.app_view(key)
            if view is not None and view["app"].get(which) and stop is not None:
                stop(key, "move", process=False)

    def _move_records(self, target: str, version: int | None) -> None:
        records = self.driver.records
        records.held = True  # keep_alive mustn't start the old cluster again while the move stops and swaps it
        try:
            self._records_move(target, version)
        finally:
            records.held = False
            self._moving.discard("records")  # the window closes however the move ends

    def _records_move(self, target: str, version: int | None) -> None:
        from ..install import find_postgres
        from .records import (
            ADMIN,
            DATABASE,
            PONDS,
            READER,
            SPOUTS,
            Endpoint,
            ManagedPostgres,
            RecordsError,
            _free_port,
            _sql_literal,
            line_role,
            parse_external,
        )

        records = self.driver.records
        lines, spout_schemas = self._schemas()
        roles = [line_role(n, m) for n, m, _ in lines]
        schemas = roles + spout_schemas
        if not schemas:  # nothing holds Records yet: only the setting changes
            records.stop()
            records._set("records", None if target == "managed" else target)
            return
        source = records.ensure()
        # Every schema the Spouts' role owns, whatever the settings say (one left by a removed Spout moves too).
        owned = records.psql(f"""SELECT n.nspname FROM pg_namespace n JOIN pg_roles r ON r.oid = n.nspowner
                                 WHERE r.rolname = '{SPOUTS}'""", source).split()
        spout_schemas = sorted(set(spout_schemas) | set(owned))
        schemas = roles + spout_schemas
        # The newest tools here: pg_dump must be at least as new as the server it reads, pg_restore as its pg_dump.
        tools = find_postgres(version) if version else None
        if tools is None:
            for v in range(30, 13, -1):
                tools = find_postgres(v)
                if tools:
                    break
        if tools is None:
            raise MoveError("PostgreSQL's tools aren't on this machine: duckstring install postgres")
        bin_dir = tools[0]

        def run(argv: list[str], endpoint: Endpoint, what: str) -> None:
            import os
            import subprocess

            out = subprocess.run(argv, capture_output=True, text=True, timeout=7200,
                                 env={**os.environ, "PGPASSWORD": endpoint.admin_password})
            if out.returncode != 0:
                raise MoveError(f"{what}: {(out.stderr or out.stdout).strip()[-2000:]}")

        # 1. The target.
        self._set(state="checking", detail="the target")
        new_pg: ManagedPostgres | None = None
        if target == "managed":
            new_pg = ManagedPostgres(records.base, "pg.new", major=version or tools[1])
            shutil.rmtree(new_pg.data, ignore_errors=True)
            new_pg.initdb(records.password(ADMIN))
            port = _free_port()
            new_pg.start(port, "127.0.0.1", records.memory)
            dest = Endpoint("127.0.0.1", port, DATABASE, ADMIN, records.password(ADMIN), managed=True)
        else:
            dest = parse_external(target)
            can = records.psql("SELECT rolsuper OR (rolcreaterole AND rolcreatedb) FROM pg_roles "
                               "WHERE rolname = current_user", dest).strip()
            if can != "t":
                raise MoveError("the target's credential needs CREATEROLE and CREATEDB (or superuser)")
            names = ", ".join(_sql_literal(s) for s in schemas)
            clash = records.psql(f"SELECT string_agg(nspname, ', ') FROM pg_namespace WHERE nspname IN ({names})",
                                 dest).strip()
            ours = ", ".join(_sql_literal(r) for r in [*roles, PONDS, SPOUTS])
            clash_roles = records.psql(f"SELECT string_agg(rolname, ', ') FROM pg_roles WHERE rolname IN ({ours})",
                                       dest).strip()
            if clash or clash_roles:
                raise MoveError(f"the target already holds {clash or clash_roles}: is another Catchment using it?")

        made_on_target = False
        try:
            # 2. The window.
            self._set(state="stopping", detail="Apps with Records, and writes")
            self._moving.add("records")
            self._stop_apps("records")
            records.psql(f"""
                DO $$DECLARE r text;
                BEGIN
                  FOREACH r IN ARRAY ARRAY[{", ".join(_sql_literal(x) for x in [*roles, SPOUTS])}] LOOP
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
                      EXECUTE format('ALTER ROLE %I NOLOGIN', r);
                    END IF;
                  END LOOP;
                END$$;
                SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                WHERE usename = ANY(ARRAY[{", ".join(_sql_literal(x) for x in [*roles, SPOUTS])}]);""", source)

            # 3. Copy.
            self._set(state="copying", detail="dumping")
            work = records.base / "moves"
            work.mkdir(parents=True, exist_ok=True)
            dump = work / f"records-{_secrets.token_hex(4)}.dump"
            try:
                run([str(bin_dir / "pg_dump"), "-Fc", "-f", str(dump), "-d", source.conninfo()], source,
                    "dumping Records")
                self._set(detail="restoring")
                made_on_target = True
                records._bootstrap(dest)  # a managed target's database, and the reader and Ponds roles
                statements = []
                for role in roles:
                    pw = _sql_literal(records.password(role))
                    statements.append(f"""
                        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                          CREATE ROLE "{role}" LOGIN PASSWORD {pw};
                        END IF;""")
                pw = _sql_literal(records.password(SPOUTS))
                statements.append(f"""
                    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{SPOUTS}') THEN
                      CREATE ROLE "{SPOUTS}" LOGIN PASSWORD {pw};
                    END IF;""")
                records.psql("DO $$BEGIN " + "".join(statements) + " END$$;" +
                             "".join(f'GRANT "{READER}" TO "{r}";' for r in roles) +
                             "".join(f'GRANT "{r}" TO CURRENT_USER;' for r in [*roles, SPOUTS]), dest)
                run([str(bin_dir / "pg_restore"), "--exit-on-error", "-d", dest.conninfo(), str(dump)], dest,
                    "restoring Records on the target")
            finally:
                dump.unlink(missing_ok=True)

            # 4. Verify.
            self._set(state="verifying", detail="row counts")
            counts_sql = self._counts_sql(records, source, schemas)
            if counts_sql:
                before = records.psql(counts_sql, source)
                after = records.psql(counts_sql, dest)
                if before != after:
                    raise MoveError("the copy's row counts differ from the source's; nothing was switched")
        except BaseException:
            self._set(detail="undoing")
            self._undo_records(records, dest, new_pg, made_on_target, schemas, roles)
            for name, major, state in lines:  # the old store serves again
                records._provisioned.discard(line_role(name, major))
                if state != "retired":
                    records.provision(name, major)
            raise

        # 5. Switch.
        self._set(state="switching", detail="")
        old_managed = records.managed
        old_external = None if old_managed else parse_external(records.target)
        old_dir = None
        records.stop()
        if target == "managed":
            new_pg.stop()
            if old_managed:
                old_dir = records.base / f"pg.old-{time.strftime('%Y%m%dT%H%M%S')}"
                records.pg.data.rename(old_dir)
            new_pg.data.rename(records.base / "pg")
            for leftover in (new_pg.log_path, new_pg._started_path):
                leftover.unlink(missing_ok=True)
            records.pg = ManagedPostgres(records.base)
            records._set("records", None)
        else:
            if old_managed:
                old_dir = records.base / f"pg.old-{time.strftime('%Y%m%dT%H%M%S')}"
                records.pg.data.rename(old_dir)
                records.pg = ManagedPostgres(records.base)
            records._set("records", target)
        records._ready = None
        records._provisioned.clear()
        records._stopping = False
        for name, major, state in lines:
            records.provision(name, major)
            if state == "retired":
                records.freeze(name, major)
        for schema in spout_schemas:
            records.spout_url(schema)
        self.driver.data_version += 1  # the serving catalog attaches the new store

        # 6. Delete the old store.
        self._set(state="deleting", detail="the old store")
        try:
            if old_dir is not None:
                shutil.rmtree(old_dir, ignore_errors=True)
                if target != "managed":  # the managed cluster's own files go with it
                    for leftover in ("started.json", "postgres.log"):
                        (records.base / leftover).unlink(missing_ok=True)
            if old_external is not None:
                self._drop_ours(records, old_external, schemas, roles)
        except (RecordsError, MoveError, OSError) as e:  # the move is done; say what was left behind
            raise MoveError(f"moved, but the old store wasn't fully deleted: {e}") from None

    @staticmethod
    def _counts_sql(records, endpoint, schemas: list[str]) -> str:
        from .records import _sql_literal

        names = ", ".join(_sql_literal(s) for s in schemas)
        tables = records.psql(f"""SELECT n.nspname || '.' || c.relname FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname IN ({names}) AND c.relkind IN ('r', 'p') ORDER BY 1""", endpoint).split()
        if not tables:
            return ""
        parts = []
        for t in tables:
            schema, table = t.split(".", 1)
            parts.append(f"""SELECT '{t}' AS t, count(*) AS n FROM "{schema}"."{table}\"""")
        return " UNION ALL ".join(parts) + " ORDER BY 1"

    @staticmethod
    def _drop_ours(records, endpoint, schemas: list[str], roles: list[str]) -> None:
        from .records import PONDS, READER, SPOUTS, _sql_literal

        all_roles = [*roles, SPOUTS, PONDS, READER]
        records.psql(
            "".join(f'DROP SCHEMA IF EXISTS "{s}" CASCADE;' for s in schemas) +
            f"""SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                WHERE usename = ANY(ARRAY[{", ".join(_sql_literal(r) for r in all_roles)}]);
            DO $$DECLARE r text;
            BEGIN
              FOREACH r IN ARRAY ARRAY[{", ".join(_sql_literal(r) for r in all_roles)}] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
                  EXECUTE format('REASSIGN OWNED BY %I TO CURRENT_USER', r);
                  EXECUTE format('DROP OWNED BY %I', r);
                  EXECUTE format('DROP ROLE %I', r);
                END IF;
              END LOOP;
            END$$;""", endpoint)

    def _undo_records(self, records, dest, new_pg, made: bool, schemas: list[str], roles: list[str]) -> None:
        try:
            if new_pg is not None:
                new_pg.stop()
                shutil.rmtree(new_pg.data, ignore_errors=True)
            elif made:
                self._drop_ours(records, dest, schemas, roles)
        except Exception as e:  # pragma: no cover - reported in the log; the source is what matters
            log.warning("undoing a Records move left something on the target: %s", e)

    # ── Media ──────────────────────────────────────────────────────────────────────────────────────────

    def _check_media_request(self, target: str) -> None:
        from ..storage import is_object_uri

        target = target or "local"
        if target != "local" and not is_object_uri(target):
            raise MoveError(f"Media goes in the Catchment's own directory (local) or an object store, not {target!r}")
        if target == self.driver.media.target:
            raise MoveError("Media is already there")

    def _move_media(self, target: str) -> None:
        try:
            self._media_move(target)
        finally:
            self._moving.discard("media")

    def _media_move(self, target: str) -> None:
        from .media import Media

        target = target or "local"
        driver = self.driver
        lines = [(d["name"], d["major"]) for d in driver.app_data() if d["media"]]
        old = Media(driver.root, driver.db, target=driver.media.target)
        new = Media(driver.root, driver.db, target=target)
        if not lines:
            driver.media.configure(target)
            return

        self._set(state="checking", detail="the target")
        for line in lines:
            if new.list(*line):
                raise MoveError(f"the target already holds files for {line[0]}@{line[1]}")

        def copy(line, entries) -> None:
            for e in entries:
                new.write(*line, e["path"], _Chunks(old.read(*line, e["path"])))

        # A first pass while Apps run.
        self._set(state="copying", detail="first pass")
        first: dict = {}
        try:
            for line in lines:
                entries = old.list(*line)
                first[line] = {e["path"]: (e["size"], e["modified"]) for e in entries}
                copy(line, entries)

            # The window: what changed since.
            self._set(state="stopping", detail="Apps with Media")
            self._moving.add("media")
            self._stop_apps("media")
            self._set(state="copying", detail="what changed")
            for line in lines:
                now = old.list(*line)
                seen = first[line]
                copy(line, [e for e in now if seen.get(e["path"]) != (e["size"], e["modified"])])
                current = {e["path"] for e in now}
                for gone in [p for p in seen if p not in current]:
                    try:
                        new.delete(*line, gone)
                    except FileNotFoundError:
                        pass

            self._set(state="verifying", detail="listings")
            for line in lines:
                a = [(e["path"], e["size"]) for e in old.list(*line)]
                b = [(e["path"], e["size"]) for e in new.list(*line)]
                if a != b:
                    raise MoveError(f"{line[0]}@{line[1]}'s copy differs from the source; nothing was switched")
        except BaseException:
            for line in lines:
                try:
                    new.drop(*line)
                except Exception:  # pragma: no cover
                    pass
            raise

        self._set(state="switching", detail="")
        from .cloud import set_setting
        from .db import locked

        with locked(driver.db):
            set_setting(driver.db, "media", None if target == "local" else target)
        driver.data_version += 1

        self._set(state="deleting", detail="the old files")
        for line in lines:
            old.drop(*line)
        if old.is_local:
            base = Path(driver.root) / "media"
            for d in sorted(base.rglob("*"), reverse=True) if base.is_dir() else []:
                if d.is_dir():
                    try:
                        d.rmdir()
                    except OSError:
                        pass
        for name, major in lines:
            new.ensure_line(name, major)
