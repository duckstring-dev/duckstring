"""An Arrow Flight front door onto the serving core (plans/data-serving.md) — the columnar-native
adapter for apps / data engineers (results stream as Arrow, no row-by-row serialization). A Flight
ticket carries the SQL; ``do_get`` runs it on the sandboxed serving connection and streams the Arrow
result; ``get_flight_info`` returns the schema + a ticket for a client that negotiates first.

Auth: an ``authorization: Bearer <api-key>`` call header → the key's access level (open ⇒ full;
read/demand ⇒ the sandboxed connection). A Tap's key (plans/taps.md) gets the Tap's own connection, and each
``do_get`` applies the Tap's policy first (``get_flight_info`` only negotiates, so it doesn't).
``pyarrow.flight`` is an optional import (the ``duckstring[aws]`` stack pulls pyarrow already; a Catchment
without it simply doesn't start the Flight port).

Scope: a generic Flight query surface. Full Flight SQL protocol conformance (the standardized command
protobufs so a generic Flight SQL / JDBC-Arrow driver introspects it) is a follow-up.
"""

from __future__ import annotations

import logging
import threading

import pyarrow.flight as flight

from . import auth

log = logging.getLogger("duckstring.flight")


def _token(headers) -> str:
    for key in ("authorization", "Authorization"):
        vals = headers.get(key)
        if vals:
            v = vals[0]
            return v[7:] if v.lower().startswith("bearer ") else v
    return ""


class _AuthMiddleware(flight.ServerMiddleware):
    def __init__(self, principal):
        self.principal = principal


class _AuthMiddlewareFactory(flight.ServerMiddlewareFactory):
    def __init__(self, driver, api_key):
        self.driver, self.api_key = driver, api_key

    def start_call(self, info, headers):
        principal = auth.principal_for_key(self.driver, self.api_key, _token(headers))
        if principal is None:
            raise flight.FlightUnauthenticatedError("invalid or missing API key")
        return _AuthMiddleware(principal)


class FlightSqlServer(flight.FlightServerBase):
    kind = "flight"

    def __init__(self, driver, *, api_key: str | None = None, host: str = "127.0.0.1", port: int = 0):
        location = f"grpc://{host}:{port}"
        super().__init__(location, middleware={"auth": _AuthMiddlewareFactory(driver, api_key)})
        self.driver = driver
        self.host = host
        self._thread: threading.Thread | None = None

    def _principal(self, context) -> auth.Principal:
        mw = context.get_middleware("auth")
        return mw.principal if mw is not None else auth.Principal(level=auth.Level.FULL, is_duck=False)

    def _arrow(self, principal: auth.Principal, sql: str):
        from .serving import _cache
        sandboxed = principal.tap is not None or principal.level < auth.Level.FULL
        cur = _cache(self.driver).connection(self.driver, sandboxed=sandboxed, tap=principal.tap).cursor()
        return cur.execute(sql).to_arrow_table()  # a materialised Table (schema + num_rows)

    def get_flight_info(self, context, descriptor):
        sql = descriptor.command.decode("utf-8")
        table = self._arrow(self._principal(context), sql)
        endpoint = flight.FlightEndpoint(flight.Ticket(sql.encode("utf-8")), [self._location])
        return flight.FlightInfo(table.schema, descriptor, [endpoint], table.num_rows, -1)

    def do_get(self, context, ticket):
        from .taps import gate_blocking

        sql = ticket.ticket.decode("utf-8")
        principal = self._principal(context)
        if principal.tap is not None:
            try:
                gate_blocking(self.driver, principal.tap)
            except KeyError:
                raise flight.FlightUnauthenticatedError("invalid or missing API key") from None
        table = self._arrow(principal, sql)
        return flight.RecordBatchStream(table)

    # ─── lifecycle (serve() blocks; run it in a thread) ────────────────────────────

    @property
    def _location(self):
        return f"grpc://127.0.0.1:{self.port}"

    def start(self) -> int:
        self._thread = threading.Thread(target=self.serve, name="ds-flight", daemon=True)
        self._thread.start()
        log.info("flight serving on port %s", self.port)
        return self.port

    def stop(self) -> None:
        try:
            self.shutdown()
        except Exception:
            pass
