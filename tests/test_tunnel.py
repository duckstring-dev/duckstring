"""Byte streams over one message transport (duckstring/tunnel.py, catchment/tunnels.py): the Catchment's loopback
port reaches a server on the agent's side, many connections at once, large transfers held to the window, ends
closing cleanly, and a refused connection reset."""

from __future__ import annotations

import asyncio
import os

import pytest

from duckstring.catchment.tunnels import TunnelSession
from duckstring.tunnel import WINDOW_BYTES, Mux


async def _pair(app_port: int, *, delay: float = 0.0, small_buffers: bool = False):
    """A session (the Catchment's end) joined to an agent's Mux by two queues standing in for a WebSocket."""
    to_agent: asyncio.Queue = asyncio.Queue()
    to_catchment: asyncio.Queue = asyncio.Queue()
    controls: list[dict] = []
    session = TunnelSession(1, controls.append)
    await session.listen()

    async def send_up(data: bytes):
        if delay:
            await asyncio.sleep(delay)
        to_catchment.put_nowait(data)

    async def send_down(data: bytes):
        to_agent.put_nowait(data)

    async def connect():
        if not small_buffers:
            return await asyncio.open_connection("127.0.0.1", app_port)
        import socket

        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)  # the kernel can't absorb the stream
        sock.setblocking(False)
        await asyncio.get_running_loop().sock_connect(sock, ("127.0.0.1", app_port))
        return await asyncio.open_connection(sock=sock)

    agent = Mux(send_up, on_open=connect)

    async def agent_loop():
        while True:
            data = await to_agent.get()
            if data is None:
                return
            await agent.feed(data)

    serving = asyncio.ensure_future(session.serve(to_catchment.get, send_down))
    pumping = asyncio.ensure_future(agent_loop())
    await asyncio.wait_for(session.connected.wait(), 2)
    return session, agent, controls, (to_agent, to_catchment, serving, pumping)


async def _close(session, parts):
    to_agent, to_catchment, serving, pumping = parts
    to_agent.put_nowait(None)
    to_catchment.put_nowait(None)
    await asyncio.gather(serving, pumping)
    await session.close()


async def _echo_server():
    async def handle(reader, writer):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def test_streams_carry_bytes_both_ways():
    async def main():
        server, port = await _echo_server()
        session, agent, _, parts = await _pair(port)

        async def one(n: int):
            reader, writer = await asyncio.open_connection("127.0.0.1", session.port)
            payload = os.urandom(n)
            writer.write(payload)
            await writer.drain()
            writer.write_eof()
            got = await reader.read()
            writer.close()
            return got == payload

        sizes = [1, 1000, WINDOW_BYTES * 3 + 17, 5 * 2**20] + [4096] * 20
        assert all(await asyncio.gather(*(one(n) for n in sizes)))
        await asyncio.sleep(0.05)
        assert not agent.streams and not session.mux.streams  # every stream ended on both sides
        await _close(session, parts)
        server.close()

    asyncio.run(asyncio.wait_for(main(), 30))


def test_a_slow_reader_is_held_to_the_window():
    async def main():
        got = asyncio.Queue()

        async def slow(reader, writer):  # reads nothing until told
            await got.get()
            data = await reader.read()
            writer.write(str(len(data)).encode())
            await writer.drain()
            writer.close()

        import socket

        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8192)  # inherited by the accepted socket
        listener.bind(("127.0.0.1", 0))
        server = await asyncio.start_server(slow, sock=listener)
        session, agent, _, parts = await _pair(server.sockets[0].getsockname()[1], small_buffers=True)
        reader, writer = await asyncio.open_connection("127.0.0.1", session.port)
        writer.write(os.urandom(8 * WINDOW_BYTES))
        for _ in range(200):  # until the window is spent (waiting on the condition: a busy machine is slow)
            await asyncio.sleep(0.025)
            streams = list(session.mux.streams.values())
            if streams and streams[0].credit <= 0:
                break
        (stream,) = session.mux.streams.values()
        assert stream.credit <= 0  # held at the window, not buffered whole in the tunnel
        await asyncio.sleep(0.2)
        assert stream.credit <= 0 and stream.inbox.qsize() == 0  # and stays held: nothing more crossed
        got.put_nowait(True)
        await writer.drain()
        writer.write_eof()
        assert int(await reader.read()) == 8 * WINDOW_BYTES
        writer.close()
        await _close(session, parts)
        server.close()

    asyncio.run(asyncio.wait_for(main(), 30))


def test_a_refused_connection_is_reset():
    async def main():
        server, port = await _echo_server()
        server.close()
        await server.wait_closed()
        session, agent, _, parts = await _pair(port)  # nothing listens there now
        reader, writer = await asyncio.open_connection("127.0.0.1", session.port)
        assert await reader.read() == b""
        writer.close()
        await asyncio.sleep(0.05)
        assert not session.mux.streams
        await _close(session, parts)

    asyncio.run(asyncio.wait_for(main(), 10))


def test_control_messages_and_a_dropped_tunnel():
    async def main():
        server, port = await _echo_server()
        session, agent, controls, parts = await _pair(port)
        await agent.control({"type": "ready"})
        await asyncio.sleep(0.05)
        assert controls == [{"type": "ready"}]
        reader, writer = await asyncio.open_connection("127.0.0.1", session.port)
        writer.write(b"x")
        assert await reader.read(1) == b"x"
        to_agent, to_catchment, serving, pumping = parts
        to_catchment.put_nowait(None)  # the WebSocket ends
        await serving
        assert not session.connected.is_set()
        assert await reader.read() == b""  # the stream failed with it
        writer.close()
        to_agent.put_nowait(None)
        await pumping
        await session.close()
        server.close()

    asyncio.run(asyncio.wait_for(main(), 10))


@pytest.mark.parametrize("delay", [0.002])
def test_a_slow_link_still_delivers_in_order(delay):
    async def main():
        server, port = await _echo_server()
        session, agent, _, parts = await _pair(port, delay=delay)
        reader, writer = await asyncio.open_connection("127.0.0.1", session.port)
        payload = os.urandom(3 * WINDOW_BYTES)
        writer.write(payload)
        writer.write_eof()
        assert await reader.read() == payload
        writer.close()
        await _close(session, parts)
        server.close()

    asyncio.run(asyncio.wait_for(main(), 30))
