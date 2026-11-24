"""Byte streams over one WebSocket (plans/app-scaling.md): how requests reach an App instance on another machine.

The instance's agent dials the Catchment, as a Duck does, and holds one WebSocket. Each TCP connection the Catchment
makes to the instance (an HTTP request, a browser's WebSocket) becomes a **stream** on it: the Catchment opens it,
the agent connects it to the App's port, and bytes flow both ways until each side has closed. Stream 0 carries JSON
control messages (configuration, readiness, freshness, stop).

A frame is a 5-byte header, the stream id (unsigned 32-bit) and a type, then the payload:

- ``OPEN``: the Catchment opens a stream (no payload).
- ``DATA``: bytes for the stream.
- ``CLOSE``: the sender has nothing more to send (end of file); the stream ends once both sides have closed.
- ``RESET``: the stream failed; both ends drop it.
- ``WINDOW``: the receiver took this many bytes (unsigned 32-bit) off the stream, so the sender may send more.
- ``CONTROL``: a JSON message, on stream 0.

**Backpressure.** Each side may have at most ``WINDOW`` bytes of a stream unacknowledged; the receiver acknowledges
bytes once its socket has taken them. So one slow reader holds back only its own stream, never the WebSocket.

This module is transport-agnostic: :class:`Mux` takes an async ``send`` for whole frames and is fed received frames
with :meth:`Mux.feed`. The Catchment's end (a loopback listener per instance) is in ``catchment/tunnels.py``; the
agent's in ``app_agent.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

OPEN, DATA, CLOSE, RESET, WINDOW, CONTROL = 1, 2, 3, 4, 5, 6
_HEADER = struct.Struct("!IB")
WINDOW_BYTES = 256 * 1024  # unacknowledged bytes per stream and direction
CHUNK = 64 * 1024  # largest DATA payload


def frame(stream: int, kind: int, payload: bytes = b"") -> bytes:
    return _HEADER.pack(stream, kind) + payload


class _Stream:
    def __init__(self, mux: Mux, sid: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.mux, self.id, self.reader, self.writer = mux, sid, reader, writer
        self.credit = WINDOW_BYTES
        self.more = asyncio.Event()
        self.more.set()
        self.sent_close = False  # our side reached end of file
        self.got_close = False  # the other side did
        self.pump: asyncio.Task | None = None
        self.inbox: asyncio.Queue[bytes | None] = asyncio.Queue()  # what the other side sent, in order
        self.drain: asyncio.Task | None = None

    def start(self) -> None:
        self.pump = asyncio.ensure_future(self._pump())
        self.drain = asyncio.ensure_future(self._drain())

    async def _pump(self) -> None:
        """Local socket to the tunnel, within the window."""
        try:
            while True:
                if self.credit <= 0:
                    self.more.clear()
                    await self.more.wait()
                    continue
                data = await self.reader.read(min(CHUNK, self.credit))
                if not data:
                    break
                self.credit -= len(data)
                await self.mux.send(frame(self.id, DATA, data))
            self.sent_close = True
            await self.mux.send(frame(self.id, CLOSE))
            self._maybe_done()
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError):
            await self.mux.reset(self.id)
        except Exception:  # the tunnel went away: the mux drops every stream
            self.mux.drop(self.id)

    async def _drain(self) -> None:
        """The tunnel to the local socket, acknowledging what it took."""
        try:
            while True:
                data = await self.inbox.get()
                if data is None:  # the other side closed
                    if self.writer.can_write_eof():
                        self.writer.write_eof()
                    self._maybe_done()
                    return
                self.writer.write(data)
                await self.writer.drain()
                await self.mux.send(frame(self.id, WINDOW, struct.pack("!I", len(data))))
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError):
            await self.mux.reset(self.id)
        except Exception:
            self.mux.drop(self.id)

    def _maybe_done(self) -> None:
        if self.sent_close and self.got_close:
            self.mux.drop(self.id)

    def close(self) -> None:
        for task in (self.pump, self.drain):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        try:
            self.writer.close()
        except Exception:
            pass


class Mux:
    """Streams over one message transport. ``send`` sends one frame; ``on_control`` gets each control message;
    ``on_open`` (the agent's side) connects a stream the other end opened and returns its reader and writer."""

    def __init__(self, send: Callable[[bytes], Awaitable[None]], *,
                 on_control: Callable[[dict], Awaitable[None] | None] | None = None,
                 on_open: Callable[[], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]] | None = None):
        self._send = send
        self._send_lock = asyncio.Lock()
        self.on_control = on_control
        self.on_open = on_open
        self.streams: dict[int, _Stream] = {}
        self._next = 1
        self.closed = False

    async def send(self, data: bytes) -> None:
        if self.closed:
            raise ConnectionError("the tunnel is closed")
        async with self._send_lock:
            await self._send(data)

    async def control(self, message: dict) -> None:
        await self.send(frame(0, CONTROL, json.dumps(message).encode()))

    async def open(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> int:
        """Carry a local connection to the other end (the Catchment's side)."""
        sid = self._next
        self._next += 1
        stream = _Stream(self, sid, reader, writer)
        self.streams[sid] = stream
        await self.send(frame(sid, OPEN))
        stream.start()
        return sid

    async def reset(self, sid: int) -> None:
        self.drop(sid)
        try:
            await self.send(frame(sid, RESET))
        except Exception:
            pass

    def drop(self, sid: int) -> None:
        stream = self.streams.pop(sid, None)
        if stream is not None:
            stream.close()

    def close(self) -> None:
        """The transport ended: every stream fails."""
        self.closed = True
        for sid in list(self.streams):
            self.drop(sid)

    async def feed(self, data: bytes) -> None:
        if len(data) < _HEADER.size:
            return
        sid, kind = _HEADER.unpack_from(data)
        payload = data[_HEADER.size:]
        if kind == CONTROL:
            if self.on_control is not None:
                result = self.on_control(json.loads(payload))
                if asyncio.iscoroutine(result):
                    await result
            return
        if kind == OPEN:
            await self._accept(sid)
            return
        stream = self.streams.get(sid)
        if stream is None:
            if kind == DATA:
                await self.send(frame(sid, RESET))
            return
        if kind == DATA:
            stream.inbox.put_nowait(payload)
        elif kind == WINDOW:
            (n,) = struct.unpack("!I", payload)
            stream.credit += n
            stream.more.set()
        elif kind == CLOSE:
            stream.got_close = True
            stream.inbox.put_nowait(None)
        elif kind == RESET:
            self.drop(sid)

    async def _accept(self, sid: int) -> None:
        if self.on_open is None:
            await self.send(frame(sid, RESET))
            return
        try:
            reader, writer = await self.on_open()
        except Exception as e:
            log.info("tunnel: stream %s refused: %s", sid, e)
            await self.send(frame(sid, RESET))
            return
        stream = _Stream(self, sid, reader, writer)
        self.streams[sid] = stream
        stream.start()
