"""The App agent (plans/app-scaling.md): one App instance on a machine other than the Catchment's.

A launcher starts it as it starts a Duck (``python -m duckstring.app_agent``, the same arguments, ``--pond``
carrying ``{name}~{instance}``). It fetches the App's code over the Duck channel when this machine doesn't have it,
dials the Catchment's tunnel (``duckstring/tunnel.py``) and waits for its configuration, builds the App's
environment, starts ``[app] command`` with the environment a Catchment instance gets (the data root, the line's
Records URL and Media in an object store, all reached directly), and reports ready once ``health`` answers. Then it
carries the Catchment's connections to the App's port, forwards the App's output as its log, the freshness the
Catchment sends, the App's holds, and its own heartbeat.

It ends when the Catchment says stop (``SIGTERM`` to the App's process group, ``SIGKILL`` after the grace), when
the App exits (reported with its code), or after ``DUCKSTRING_DUCK_ORPHAN_MINUTES`` without the Catchment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path

from .keys import split_instance
from .tunnel import Mux

log = logging.getLogger("duckstring.app_agent")

HEARTBEAT_S = 2.0
RECONNECT_MAX_S = 10.0
DEFAULT_ORPHAN_MINUTES = 60.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tunnel_url(base: str, name: str, major: int, instance: int) -> str:
    base = base.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return f"{base}/api/duck/{name}/{major}/app/{instance}/tunnel"


class Agent:
    def __init__(self, *, name: str, major: int, instance: int, version: str, catchment: str, token: str,
                 root: Path, source_path: str, audience: str | None = None):
        self.name, self.major, self.instance, self.version = name, major, instance, version
        self.catchment, self.token, self.root = catchment, token, Path(root)
        self.source_dir = self.root / source_path
        self.audience = audience
        self.runtime = self.root / "ponds" / name / f"m{major}" / "app"
        self.mux: Mux | None = None
        self.config: asyncio.Future | None = None
        self.stop_requested: asyncio.Event | None = None
        self.stop_grace = 10.0
        self.proc: asyncio.subprocess.Process | None = None
        self.port = 0
        self.last_contact = time.monotonic()
        self.connected = False
        self._holds: dict | None = None
        self._log_buffer: list[str] = []

    # ─── the tunnel ──────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        headers = {"X-Duck-Token": self.token}
        from .duck.id_token import HEADER, catchment_auth

        auth = catchment_auth(self.catchment, self.audience)
        token = auth.token() if auth is not None else None
        if token:
            headers[HEADER] = f"Bearer {token}"
        return headers

    async def _tunnel_loop(self) -> None:
        """Dial the Catchment and carry the tunnel, again after any drop, until the agent stops."""
        from websockets.asyncio.client import connect

        url = tunnel_url(self.catchment, self.name, self.major, self.instance)
        delay = 0.5
        while not self.stop_requested.is_set():
            try:
                async with connect(url, additional_headers=self._headers(), max_size=None,
                                   ping_interval=20, ping_timeout=20, open_timeout=15) as ws:
                    delay = 0.5
                    await self._carry(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status in (403, 404, 410):  # the Catchment doesn't know this instance (any more)
                    log.warning("the Catchment refused instance %s (%s): stopping", self.instance, status)
                    self.stop_requested.set()
                    return
                log.info("tunnel to %s: %s", self.catchment, e)
            self.connected = False
            await asyncio.sleep(delay)
            delay = min(RECONNECT_MAX_S, delay * 2)

    async def _carry(self, ws) -> None:
        async def send(data: bytes) -> None:
            await ws.send(data)

        mux = Mux(send, on_control=self._on_control, on_open=self._open_app)
        self.mux = mux
        self.connected = True
        self.last_contact = time.monotonic()
        await mux.control({"type": "hello", "instance": self.instance, "version": self.version,
                           "state": self._state()})
        try:
            async for message in ws:
                if isinstance(message, str):
                    continue
                self.last_contact = time.monotonic()
                await mux.feed(message)
        finally:
            mux.close()
            if self.mux is mux:
                self.mux = None

    def _state(self) -> str:
        if self.proc is None:
            return "booting"
        return "running" if self.proc.returncode is None else "exited"

    async def _open_app(self):
        if not self.port:
            raise ConnectionRefusedError("the App hasn't started")
        return await asyncio.open_connection("127.0.0.1", self.port)

    async def control(self, message: dict) -> None:
        mux = self.mux
        if mux is None or mux.closed:
            return
        try:
            await mux.control(message)
        except Exception:
            pass

    def _on_control(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "config":
            if self.config is not None and not self.config.done():
                self.config.set_result(message)
        elif kind == "freshness":
            self._write_json("freshness.json", message.get("data") or {})
        elif kind == "stop":
            self.stop_grace = float(message.get("grace") or self.stop_grace)
            self.stop_requested.set()

    def _write_json(self, name: str, data: dict) -> None:
        self.runtime.mkdir(parents=True, exist_ok=True)
        tmp = self.runtime / f"{name}.tmp"
        tmp.write_text(json.dumps(data))
        tmp.replace(self.runtime / name)

    # ─── running the App ─────────────────────────────────────────────────────

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        self.config = loop.create_future()
        self.stop_requested = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.stop_requested.set)
            except (NotImplementedError, RuntimeError):  # pragma: no cover - not on this platform
                pass
        tunnel = asyncio.ensure_future(self._tunnel_loop())
        heartbeat = asyncio.ensure_future(self._heartbeat())
        code = 0
        try:
            config = await self._wait(self.config)
            if config is None:
                return 0
            python = await self._environment()
            if python is None:
                return 1
            code = await self._serve(config, python)
        finally:
            heartbeat.cancel()
            await self._flush_log()
            await self.control({"type": "exited", "code": code})
            await asyncio.sleep(0.1)
            tunnel.cancel()
            await asyncio.gather(tunnel, heartbeat, return_exceptions=True)
        # Stopped as asked: a clean exit, whatever signal ended the App. On its own: the App's code (0 to 255).
        return 0 if self.stop_requested.is_set() else (code if 0 <= code <= 255 else 1)

    async def _wait(self, fut):
        """``fut``'s result, or ``None`` once a stop is asked for first."""
        stop = asyncio.ensure_future(self.stop_requested.wait())
        done, _ = await asyncio.wait({fut, stop}, return_when=asyncio.FIRST_COMPLETED)
        stop.cancel()
        return fut.result() if fut in done else None

    async def _environment(self) -> Path | None:
        """The App's environment on this machine, built now if need be (the Catchment hears a heartbeat meanwhile)."""
        from .environments import ensure_env

        try:
            return await asyncio.to_thread(ensure_env, self.root, self.source_dir, check_lock=False)
        except Exception as exc:
            detail = str(exc).strip()
            last = detail.splitlines()[-1] if detail else type(exc).__name__
            log.error("building the App's environment failed: %s", detail)
            self._log_buffer.append(f"[duckstring] building the App's environment failed:\n{detail}\n")
            await self.control({"type": "failed", "error": f"Building the App's environment failed: {last}"[:500]})
            return None

    def _app_env(self, config: dict, python: Path) -> dict[str, str]:
        from .app import ENV, RECORDS_ENV

        env = {k: v for k, v in os.environ.items() if k != "DUCKSTRING_API_KEY"}
        env.update(config.get("env") or {})
        env["PATH"] = str(Path(python).parent) + os.pathsep + env.get("PATH", "")
        env["PORT"] = str(self.port)
        env["PYTHONUNBUFFERED"] = "1"
        ca = config.get("records_ca")
        if ca and env.get(RECORDS_ENV):
            path = self.runtime / "records-ca.pem"
            path.write_text(ca)
            os.chmod(path, 0o600)
            sep = "&" if "?" in env[RECORDS_ENV] else "?"
            env[RECORDS_ENV] += f"{sep}sslrootcert={path}"
        app = dict(config.get("app") or {})
        app.update({"root": str(self.root), "runtime": str(self.runtime)})
        env[ENV] = json.dumps(app)
        return env

    async def _serve(self, config: dict, python: Path) -> int:
        self.runtime.mkdir(parents=True, exist_ok=True)
        self._write_json("freshness.json", config.get("freshness") or {})
        self.port = _free_port()
        self.stop_grace = float(config.get("stop_grace") or self.stop_grace)
        env = self._app_env(config, python)
        self._log_buffer.append(f"[duckstring] starting {self.name}@{self.major} {self.version} on "
                                f"{socket.gethostname()} (instance {self.instance})\n")
        self.proc = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c", "exec " + config["command"], cwd=self.source_dir, env=env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        output = asyncio.ensure_future(self._read_output())
        try:
            ready = await self._ready(config)
            if ready:
                await self.control({"type": "ready"})
            exited = asyncio.ensure_future(self.proc.wait())
            await self._wait(exited)
            if self.proc.returncode is None:  # a stop was asked for
                await self._stop_app()
            await output
            return self.proc.returncode if self.proc.returncode is not None else 0
        finally:
            output.cancel()

    async def _ready(self, config: dict) -> bool:
        import httpx

        health = config.get("health")
        url = f"http://127.0.0.1:{self.port}{config.get('health_prefix') or ''}{health}" if health else None
        deadline = time.monotonic() + float(config.get("start_timeout") or 60.0)
        async with httpx.AsyncClient(timeout=2.0) as client:
            while time.monotonic() < deadline and not self.stop_requested.is_set():
                if self.proc.returncode is not None:
                    return False
                try:
                    if url is None:
                        _, writer = await asyncio.open_connection("127.0.0.1", self.port)
                        writer.close()
                        return True
                    r = await client.get(url)
                    if 200 <= r.status_code < 300:
                        return True
                except Exception:
                    pass
                await asyncio.sleep(0.1)
        return False

    async def _stop_app(self) -> None:
        proc = self.proc
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), self.stop_grace)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            await proc.wait()

    async def _read_output(self) -> None:
        log_path = self.runtime / "logs" / f"{self.instance}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as local:
            while True:
                chunk = await self.proc.stdout.read(65536)
                if not chunk:
                    return
                local.write(chunk)
                local.flush()
                self._log_buffer.append(chunk.decode(errors="replace"))

    async def _flush_log(self) -> None:
        if self._log_buffer:
            text, self._log_buffer = "".join(self._log_buffer), []
            await self.control({"type": "log", "text": text[-200_000:]})

    async def _heartbeat(self) -> None:
        """Every ``HEARTBEAT_S``: the App's output since the last, its holds when they change, a sign of life; and
        the orphan rule."""
        orphan_s = _orphan_s()
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            if self.connected:
                self.last_contact = time.monotonic()
                await self._flush_log()
                holds = self._read_holds()
                if holds != self._holds:
                    self._holds = holds
                    await self.control({"type": "holds", "sources": holds})
                await self.control({"type": "alive", "state": self._state()})
            elif orphan_s is not None and time.monotonic() - self.last_contact > orphan_s:
                log.warning("no contact with the Catchment for %.0f minutes: stopping", orphan_s / 60)
                self.stop_requested.set()

    def _read_holds(self) -> dict[str, list[str]]:
        """The App's ``pond.hold()`` files: each live process's held versions, by Source name."""
        out: dict[str, list[str]] = {}
        holds = self.runtime / "holds"
        for path in sorted(holds.glob("*.json")) if holds.is_dir() else ():
            try:
                os.kill(int(path.stem.split("-")[0]), 0)
                sources = json.loads(path.read_text()).get("sources", {})
            except (ValueError, OSError):
                continue
            for name, f in sources.items():
                if f:
                    out.setdefault(name, []).append(f)
        return out


def _orphan_s() -> float | None:
    raw = os.environ.get("DUCKSTRING_DUCK_ORPHAN_MINUTES", str(DEFAULT_ORPHAN_MINUTES))
    try:
        minutes = float(raw)
    except ValueError:
        minutes = DEFAULT_ORPHAN_MINUTES
    return minutes * 60 if minutes > 0 else None


def main() -> None:
    ap = argparse.ArgumentParser(prog="duckstring.app_agent")
    ap.add_argument("--pond", required=True, help="{name}~{instance}")
    ap.add_argument("--major", required=True, type=int)
    ap.add_argument("--version", required=True)
    ap.add_argument("--catchment", required=True)
    ap.add_argument("--token", default="")
    ap.add_argument("--root", required=True)
    ap.add_argument("--source-path", required=True)
    ap.add_argument("--data-root", default="")  # accepted as a Duck's are; the configuration carries it
    ap.add_argument("--persist-root", default="")
    ap.add_argument("--audience", default="")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    name, instance = split_instance(args.pond)
    if instance is None:
        ap.error("--pond must name the instance: {name}~{instance}")
    root = Path(args.root)
    source_dir = root / args.source_path
    if not source_dir.is_dir():
        from .duck.__main__ import _unpack_artifact
        from .duck.client import CatchmentClient

        client = CatchmentClient(args.catchment, name, args.major, args.token, version=args.version,
                                 audience=args.audience or None)
        try:
            _unpack_artifact(client.fetch_artifact(), source_dir)
        finally:
            client.close()
        log.info("[%s@%s] fetched the App's code -> %s", name, args.major, source_dir)
    agent = Agent(name=name, major=args.major, instance=instance, version=args.version, catchment=args.catchment,
                  token=args.token, root=root, source_path=args.source_path, audience=args.audience or None)
    sys.exit(asyncio.run(agent.run()))


if __name__ == "__main__":
    main()
