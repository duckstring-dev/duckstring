"""The Catchment's end of App instance tunnels (plans/app-scaling.md; the protocol is ``duckstring/tunnel.py``).

An App instance on another machine is reached through the WebSocket its agent dials. For each such instance the
Catchment listens on a loopback port: a connection to it becomes a stream to the App's port on the instance, so
the App proxy forwards HTTP and WebSockets to ``127.0.0.1:{port}`` exactly as it does for an instance on this
machine. When the tunnel drops, new connections wait up to ``RECONNECT_WAIT_S`` for the agent to dial again,
then are refused.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from ..tunnel import Mux

log = logging.getLogger(__name__)

RECONNECT_WAIT_S = 10.0


class TunnelSession:
    def __init__(self, instance: int, on_control: Callable[[dict], None]):
        self.instance = instance
        self.on_control = on_control
        self.mux: Mux | None = None
        self.connected = asyncio.Event()
        self.server: asyncio.Server | None = None
        self.port = 0
        self.last_contact = 0.0  # loop time of the agent's last frame
        self.closed = False

    async def listen(self) -> None:
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.wait_for(self.connected.wait(), RECONNECT_WAIT_S)
        except asyncio.TimeoutError:
            writer.close()
            return
        mux = self.mux
        if mux is None or mux.closed:
            writer.close()
            return
        try:
            await mux.open(reader, writer)
        except Exception:
            writer.close()

    async def control(self, message: dict) -> bool:
        """Send a control message; ``False`` when the agent isn't connected."""
        mux = self.mux
        if mux is None or mux.closed:
            return False
        try:
            await mux.control(message)
            return True
        except Exception:
            return False

    async def serve(self, receive, send) -> None:
        """Carry the tunnel over one WebSocket (``receive`` returns the next binary message, ``None`` at the end;
        ``send`` sends one) until it ends. A newer connection from the same agent replaces an older one."""
        loop = asyncio.get_running_loop()
        old = self.mux
        if old is not None:
            old.close()
        mux = Mux(send, on_control=self._control)
        self.mux = mux
        self.last_contact = loop.time()
        self.connected.set()
        try:
            while True:
                data = await receive()
                if data is None:
                    break
                self.last_contact = loop.time()
                await mux.feed(data)
        finally:
            mux.close()
            if self.mux is mux:
                self.mux = None
                self.connected.clear()

    def _control(self, message: dict) -> None:
        try:
            self.on_control(message)
        except Exception:
            log.exception("tunnel %s: control message %s", self.instance, message.get("type"))

    async def close(self) -> None:
        self.closed = True
        if self.mux is not None:
            self.mux.close()
        if self.server is not None:
            self.server.close()


class TunnelHub:
    """The open tunnel sessions, by App instance id."""

    def __init__(self):
        self.sessions: dict[int, TunnelSession] = {}

    async def create(self, instance: int, on_control: Callable[[dict], None]) -> TunnelSession:
        session = TunnelSession(instance, on_control)
        await session.listen()
        self.sessions[instance] = session
        return session

    def get(self, instance: int) -> TunnelSession | None:
        return self.sessions.get(instance)

    async def close(self, instance: int) -> None:
        session = self.sessions.pop(instance, None)
        if session is not None:
            await session.close()

    async def close_all(self) -> None:
        for instance in list(self.sessions):
            await self.close(instance)
