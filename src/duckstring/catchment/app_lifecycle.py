"""The major lines of an App with Records or Media (plans/records-and-media.md): trial, live and retired.

An App's Records and Media are the original data, which nothing recomputes, so an App holding either has one live
major at a time: two would take writes in two places. Its lines are:

- **trial**: a new major while another is live. Its first start copies the live line's Records and Media and runs
  ``[app] migrate`` over the copy; anything written there is discarded by the next migration (``duckstring app
  migrate`` runs it again). Manage access only.
- **live**: the major ``/apps/{name}`` serves.
- **retired**: the previous major after a promotion. It never starts, its address redirects to the live major, and
  its Records are frozen (its role can't sign in) until purged.

``pond promote`` on such an App is the cutover: while it runs the App answers 503; the live line's instances
stop and its role is frozen, the migration runs (copy, then ``migrate``), the new major goes live and the old one
is retired. A failed migration leaves the old line live and unfrozen.

An App without Records or Media keeps concurrent majors, as before.
"""

from __future__ import annotations

import os
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path

from ..keys import pond_key

MIGRATE_TIMEOUT_S = 600.0
CUTOVER_RETRY_S = 5


class LifecycleError(Exception):
    pass


def stateful(cfg: dict | None) -> bool:
    return bool(cfg and (cfg.get("records") or cfg.get("media")))


class Lifecycle:
    """The trial/live/retired rules, the migration and the cutover, for one Catchment."""

    def __init__(self, driver, root: Path):
        self.driver = driver
        self.root = Path(root)
        self._cutovers: set[str] = set()
        self._mu = threading.Lock()

    # ── states ─────────────────────────────────────────────────────────────────────────────────────────

    def state(self, name: str, major: int) -> str | None:
        line = next((d for d in self.driver.app_data(name) if d["major"] == major), None)
        return line["state"] if line else None

    def live_major(self, name: str) -> int | None:
        return next((d["major"] for d in self.driver.app_data(name) if d["state"] == "live"), None)

    def source_major(self, name: str, major: int) -> int | None:
        """The line a new stateful line ``major`` takes its data from: the live one, else the newest other deployed
        major keeping a private state directory (``state = "local"``, no Records or Media: an App moving its own
        SQLite into Records), else None."""
        live = self.live_major(name)
        if live is not None:
            return live if live != major else None
        with self.driver.lock:
            majors = self.driver.deployed_majors(name)
        legacy = []
        for m in majors:
            if m == major or self.state(name, m) is not None:
                continue
            v = self.driver.app_view(pond_key(name, m))
            if v is not None and v["app"].get("state") == "local" and not stateful(v["app"]):
                legacy.append(m)
        return max(legacy) if legacy else None

    def cutting_over(self, name: str) -> bool:
        return name in self._cutovers

    @contextmanager
    def _cutover(self, name: str):
        with self._mu:
            if name in self._cutovers:
                raise LifecycleError(f"'{name}' is already being promoted")
            self._cutovers.add(name)
        try:
            yield
        finally:
            with self._mu:
                self._cutovers.discard(name)

    def _set_state(self, name: str, major: int, state: str) -> None:
        driver = self.driver
        with driver.lock:
            driver.db.execute("UPDATE app_data SET state = ? WHERE name = ? AND major = ?", (state, name, int(major)))
            driver.db.commit()
            driver.data_version += 1

    # ── a line's first start ───────────────────────────────────────────────────────────────────────────

    def prepare(self, view: dict) -> str:
        """Before an instance of a stateful line starts: a line not seen before is live, or a trial (migrated from
        the live line) when another is live. Returns the line's state."""
        name, major = view["name"], view["major"]
        known = self.state(name, major)
        if known is not None:
            return known
        source = self.source_major(name, major)
        state = "trial" if source is not None else "live"
        cfg = view["app"]
        self.driver.note_app_data(name, major, records=bool(cfg.get("records")), media=bool(cfg.get("media")),
                                  state=state)
        if state == "trial":
            self.migrate(view, source)
        return state

    # ── migration ──────────────────────────────────────────────────────────────────────────────────────

    def migrate(self, view: dict, source_major: int) -> None:
        """Replace the line's Records and Media with copies of ``source_major``'s, then run its ``[app] migrate``
        over them. Raises :class:`LifecycleError`."""
        from .media import MediaError
        from .records import RecordsError

        name, major = view["name"], view["major"]
        cfg = view["app"]
        source = next((d for d in self.driver.app_data(name) if d["major"] == source_major), None)
        try:
            if cfg.get("records"):
                if source and source["records"]:
                    self.driver.records.copy_line((name, source_major), (name, major))
                else:
                    self.driver.records.provision(name, major)
            if cfg.get("media"):
                if source and source["media"]:
                    self.driver.media.copy_line((name, source_major), (name, major))
                else:
                    self.driver.media.ensure_line(name, major)
        except (RecordsError, MediaError) as e:
            raise LifecycleError(f"copying {name}@{source_major}'s data into v{major}: {e}") from None
        if cfg.get("migrate"):
            self._run_migrate(view, source_major)

    def _run_migrate(self, view: dict, source_major: int) -> None:
        from ..app import MEDIA_ENV, RECORDS_ENV
        from ..environments import ensure_env
        from .app_supervisor import _WITHHELD_ENV, app_state_dir

        name, major = view["name"], view["major"]
        cfg = view["app"]
        python = ensure_env(self.root, self.root / view["source_path"], check_lock=False)
        env = {k: v for k, v in os.environ.items() if k not in _WITHHELD_ENV}
        env["PATH"] = str(Path(python).parent) + os.pathsep + env.get("PATH", "")
        env["DUCKSTRING_PREVIOUS_MAJOR"] = str(source_major)
        previous_state = app_state_dir(self.root, name, source_major)
        if previous_state.is_dir():  # a previous major's private directory (moving its SQLite into Records)
            env["DUCKSTRING_PREVIOUS_STATE"] = str(previous_state)
        if cfg.get("records"):
            env[RECORDS_ENV] = self.driver.records.line_url(name, major)
        if cfg.get("media"):
            env[MEDIA_ENV] = self.driver.media.location(name, major)
            env["DUCKSTRING_PREVIOUS_MEDIA_URL"] = self.driver.media.location(name, source_major)
        try:
            done = subprocess.run(
                ["/bin/sh", "-c", "exec " + cfg["migrate"]], cwd=self.root / view["source_path"], env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=MIGRATE_TIMEOUT_S, start_new_session=True)
        except subprocess.TimeoutExpired as exc:
            tail = (exc.stdout or b"").decode(errors="replace")[-2000:]
            raise LifecycleError(f"{name}@{major}'s migrate command didn't finish within {MIGRATE_TIMEOUT_S:.0f}s"
                                 f"\n{tail}") from None
        if done.returncode != 0:
            tail = done.stdout.decode(errors="replace")[-2000:]
            raise LifecycleError(f"{name}@{major}'s migrate command exited with code {done.returncode}\n{tail}")

    def rerun_trial(self, supervisor, name: str, major: int) -> None:
        """Migrate a trial line again from the live one (discarding what was written there)."""
        if self.state(name, major) != "trial":
            raise LifecycleError(f"{name}@{major} isn't a trial: only a trial is migrated again")
        view = self.driver.app_view(pond_key(name, major))
        live = self.live_major(name)
        if view is None or live is None:
            raise LifecycleError(f"{name}@{major} or its live line isn't deployed")
        supervisor.stop_now(pond_key(name, major), "migrate", process=True)
        self.migrate(view, live)

    # ── the cutover ────────────────────────────────────────────────────────────────────────────────────

    def promote(self, supervisor, name: str, major: int) -> dict:
        """Make ``major`` the App's live line (see the module docstring). Returns what happened."""
        view = self.driver.app_view(pond_key(name, major))
        if view is None:
            raise LifecycleError(f"'{name}@{major}' is not a deployed App")
        live = self.live_major(name)
        if live == major:
            self.driver.promote(name, major)
            return {"live": major, "migrated": False}
        legacy = False
        if live is None:  # an earlier major with only a private state directory: it hands over too
            live = self.source_major(name, major)
            legacy = live is not None
        state = self.state(name, major)
        if state == "retired":
            raise LifecycleError(f"{name}@{major} is retired: its data is older than the live line's")
        moves = getattr(self.driver, "moves", None)
        if moves is not None and moves.busy():
            raise LifecycleError("Records or Media are being moved; promote once the move has finished")
        with self._cutover(name):
            old_view = self.driver.app_view(pond_key(name, live)) if live is not None else None
            if live is not None:
                supervisor.stop_now(pond_key(name, live), "cutover", process=True)
            supervisor.stop_now(pond_key(name, major), "cutover", process=True)
            old_records = live is not None and bool(old_view and old_view["app"].get("records")) and any(
                d["major"] == live and d["records"] for d in self.driver.app_data(name))
            if old_records:
                self.driver.records.freeze(name, live)
            try:
                if state is None:
                    cfg = view["app"]
                    self.driver.note_app_data(name, major, records=bool(cfg.get("records")),
                                              media=bool(cfg.get("media")), state="trial")
                if live is not None:
                    self.migrate(view, live)
            except Exception:
                if old_records:
                    self.driver.records.provision(name, live)  # signs in again: the old line stays live
                raise
            self._set_state(name, major, "live")
            if legacy:
                self.driver.note_app_data(name, live, state="retired")  # no Records or Media: only its state
            elif live is not None:
                self._set_state(name, live, "retired")
            self.driver.promote(name, major)
        return {"live": major, "retired": live, "migrated": live is not None}

    # ── purge ──────────────────────────────────────────────────────────────────────────────────────────

    def purge(self, supervisor, name: str, major: int) -> None:
        """Delete a line's Records and Media for good. Refused for a deployed live line."""
        line = next((d for d in self.driver.app_data(name) if d["major"] == major), None)
        if line is None:
            raise LifecycleError(f"{name}@{major} holds no Records or Media")
        if line["state"] == "live" and self.driver.app_view(pond_key(name, major)) is not None:
            raise LifecycleError(f"{name}@{major} is the live line: promote another major, or remove the App, first")
        supervisor.stop_now(pond_key(name, major), "purge", process=True)
        if line["records"]:
            self.driver.records.drop(name, major)
        if line["media"]:
            self.driver.media.drop(name, major)
        driver = self.driver
        with driver.lock:
            driver.db.execute("DELETE FROM app_data WHERE name = ? AND major = ?", (name, int(major)))
            driver.db.commit()
            driver.data_version += 1
