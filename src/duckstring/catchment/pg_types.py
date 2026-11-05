"""Postgres types for the pg wire: DuckDB types as Postgres type OIDs, values in both wire formats, and the
catalog answers a driver needs before it can read anything.

Drivers ask for results in binary wherever they know the type (Npgsql, which Power BI uses, asks for every
column in binary), so every OID sent here has a binary encoder. A DuckDB type with no close Postgres type goes
as ``text``, whose binary form is its UTF-8 text.

Npgsql loads the server's types on connect with queries over ``pg_type``, ``pg_proc`` and ``pg_range``. DuckDB's
``pg_catalog`` lists DuckDB's own types under DuckDB's OIDs and has no ``pg_range``, so those queries are answered
here with Postgres 14's built-in types (``catalog_answer``), matching the OIDs this module sends.
"""

from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from typing import Any, Callable

_PG_EPOCH = datetime(2000, 1, 1)
_PG_EPOCH_DATE = date(2000, 1, 1)
_US = timedelta(microseconds=1)


# ─── values → wire ───────────────────────────────────────────────────────────────

def _utc(v: datetime) -> datetime:
    return v.astimezone(timezone.utc).replace(tzinfo=None) if v.tzinfo else v


def _text(v: Any) -> bytes:
    return str(v).encode("utf-8")


def _text_bool(v: Any) -> bytes:
    return b"t" if v else b"f"


def _text_bytea(v: Any) -> bytes:
    return b"\\x" + bytes(v).hex().encode()


def _text_tstz(v: Any) -> bytes:
    return (_utc(v).isoformat(" ") + "+00").encode() if isinstance(v, datetime) else _text(v)


def _text_interval(v: Any) -> bytes:
    if not isinstance(v, timedelta):
        return _text(v)
    us = v.seconds * 1_000_000 + v.microseconds
    h, rem = divmod(us, 3_600_000_000)
    m, rem = divmod(rem, 60_000_000)
    return f"{v.days} days {h:02}:{m:02}:{rem / 1_000_000:09.6f}".encode()


def _bin_numeric(v: Any) -> bytes:
    d = Decimal(v)
    if d.is_nan():
        return struct.pack("!hhHH", 0, 0, 0xC000, 0)
    if d.is_infinite():
        return struct.pack("!hhHH", 0, 0, 0xF000 if d < 0 else 0xD000, 0)
    sign, _, exp = d.as_tuple()
    dscale = max(0, -exp)
    whole, _, frac = format(d.copy_abs(), "f").partition(".")    # copy_abs: abs() rounds to 28 digits
    whole = whole.lstrip("0")
    whole = "0" * (-len(whole) % 4) + whole
    frac = frac + "0" * (-len(frac) % 4)
    groups = [int(whole[i:i + 4]) for i in range(0, len(whole), 4)]
    weight = len(groups) - 1
    groups += [int(frac[i:i + 4]) for i in range(0, len(frac), 4)]
    while groups and groups[0] == 0:          # leading zero groups move the weight down
        groups.pop(0)
        weight -= 1
    while groups and groups[-1] == 0:
        groups.pop()
    if not groups:
        weight = 0
    head = struct.pack("!hhHH", len(groups), weight, 0x4000 if sign else 0, dscale)
    return head + struct.pack(f"!{len(groups)}h", *groups)


def _bin_date(v: Any) -> bytes:
    return struct.pack("!i", (v - _PG_EPOCH_DATE).days)


def _bin_timestamp(v: Any) -> bytes:
    return struct.pack("!q", (_utc(v) - _PG_EPOCH) // _US)


def _bin_time(v: Any) -> bytes:
    return struct.pack("!q", ((v.hour * 60 + v.minute) * 60 + v.second) * 1_000_000 + v.microsecond)


def _bin_interval(v: Any) -> bytes:
    return struct.pack("!qii", (v.seconds * 1_000_000) + v.microseconds, v.days, 0)


@dataclass(frozen=True)
class PgType:
    name: str
    oid: int
    size: int                             # pg_type.typlen: -1 for variable length
    binary: Callable[[Any], bytes]
    text: Callable[[Any], bytes] = _text


BOOL = PgType("bool", 16, 1, lambda v: b"\x01" if v else b"\x00", _text_bool)
BYTEA = PgType("bytea", 17, -1, bytes, _text_bytea)
INT8 = PgType("int8", 20, 8, lambda v: struct.pack("!q", v))
INT2 = PgType("int2", 21, 2, lambda v: struct.pack("!h", v))
INT4 = PgType("int4", 23, 4, lambda v: struct.pack("!i", v))
TEXT = PgType("text", 25, -1, _text)
JSON = PgType("json", 114, -1, _text)
FLOAT4 = PgType("float4", 700, 4, lambda v: struct.pack("!f", v))
FLOAT8 = PgType("float8", 701, 8, lambda v: struct.pack("!d", v))
VARCHAR = PgType("varchar", 1043, -1, _text)
DATE = PgType("date", 1082, 4, _bin_date)
TIME = PgType("time", 1083, 8, _bin_time)
TIMESTAMP = PgType("timestamp", 1114, 8, _bin_timestamp)
TIMESTAMPTZ = PgType("timestamptz", 1184, 8, _bin_timestamp, _text_tstz)
INTERVAL = PgType("interval", 1186, 16, _bin_interval, _text_interval)
NUMERIC = PgType("numeric", 1700, -1, _bin_numeric)
UUID = PgType("uuid", 2950, 16, lambda v: uuid.UUID(str(v)).bytes)

_BY_DUCK = {
    "BOOLEAN": BOOL, "BLOB": BYTEA, "JSON": JSON, "UUID": UUID,
    "TINYINT": INT2, "UTINYINT": INT2, "SMALLINT": INT2,
    "USMALLINT": INT4, "INTEGER": INT4,
    "UINTEGER": INT8, "BIGINT": INT8,
    # Beyond int8: exact as numeric (an integer SUM is a HUGEINT).
    "UBIGINT": NUMERIC, "HUGEINT": NUMERIC, "UHUGEINT": NUMERIC, "BIGNUM": NUMERIC, "VARINT": NUMERIC,
    "FLOAT": FLOAT4, "DOUBLE": FLOAT8,
    "VARCHAR": VARCHAR, "DATE": DATE, "TIME": TIME, "INTERVAL": INTERVAL,
    "TIMESTAMP": TIMESTAMP, "TIMESTAMP_S": TIMESTAMP, "TIMESTAMP_MS": TIMESTAMP, "TIMESTAMP_NS": TIMESTAMP,
    "TIMESTAMP WITH TIME ZONE": TIMESTAMPTZ,
}


def pg_type(duck_type: str) -> PgType:
    """The Postgres type a DuckDB column is sent as (``text`` when none fits: lists, structs, maps, unions)."""
    t = duck_type.upper()
    if t.startswith("DECIMAL"):
        return NUMERIC
    return _BY_DUCK.get(t, TEXT)


def typmod(duck_type: str) -> int:
    """pg_attribute.atttypmod: a DECIMAL's precision and scale, else -1."""
    t = duck_type.upper()
    if t.startswith("DECIMAL(") and t.endswith(")"):
        p, _, s = t[8:-1].partition(",")
        return ((int(p) << 16) | int(s or 0)) + 4
    return -1


def encode(t: PgType, v: Any, binary: bool) -> bytes | None:
    if v is None:
        return None
    return t.binary(v) if binary else t.text(v)


# ─── wire → parameter values ─────────────────────────────────────────────────────

def _numeric_from(b: bytes) -> Decimal:
    n, weight, sign, dscale = struct.unpack("!hhHH", b[:8])
    if sign == 0xC000:
        return Decimal("NaN")
    if sign in (0xD000, 0xF000):
        return Decimal("-Infinity" if sign == 0xF000 else "Infinity")
    coefficient = 0
    for group in struct.unpack(f"!{n}h", b[8:8 + 2 * n]):
        coefficient = coefficient * 10000 + group
    with localcontext() as ctx:
        ctx.prec = 1000                               # exact: a numeric has at most 1000 digits
        value = Decimal(coefficient).scaleb(4 * (weight - n + 1)).quantize(Decimal(1).scaleb(-dscale))
        return -value if sign == 0x4000 else value


_DECODERS: dict[int, Callable[[bytes], Any]] = {
    16: lambda b: b != b"\x00",
    17: bytes,
    20: lambda b: struct.unpack("!q", b)[0],
    21: lambda b: struct.unpack("!h", b)[0],
    23: lambda b: struct.unpack("!i", b)[0],
    26: lambda b: struct.unpack("!I", b)[0],
    700: lambda b: struct.unpack("!f", b)[0],
    701: lambda b: struct.unpack("!d", b)[0],
    1082: lambda b: _PG_EPOCH_DATE + timedelta(days=struct.unpack("!i", b)[0]),
    1083: lambda b: (datetime.min + timedelta(microseconds=struct.unpack("!q", b)[0])).time(),
    1114: lambda b: _PG_EPOCH + timedelta(microseconds=struct.unpack("!q", b)[0]),
    1184: lambda b: (_PG_EPOCH + timedelta(microseconds=struct.unpack("!q", b)[0])).replace(tzinfo=timezone.utc),
    1186: lambda b: (lambda us, d, mo: timedelta(days=d + 30 * mo, microseconds=us))(*struct.unpack("!qii", b)),
    1700: _numeric_from,
    2950: lambda b: uuid.UUID(bytes=b),
}


def decode_param(oid: int, binary: bool, raw: bytes) -> Any:
    """A bound parameter as a Python value. Text-format values stay strings for DuckDB to cast; a binary value is
    decoded by its declared type, and an unknown type is taken as UTF-8 text."""
    if not binary:
        return raw.decode("utf-8", "replace")
    decoder = _DECODERS.get(oid)
    return decoder(raw) if decoder else raw.decode("utf-8", "replace")


# ─── catalog answers ─────────────────────────────────────────────────────────────

# Postgres 14's built-in base types (nspname, typname, oid), as a driver's type loading expects them.
_BASE_TYPES = [
    ("bool", 16), ("bytea", 17), ("char", 18), ("name", 19), ("int8", 20), ("int2", 21), ("int4", 23),
    ("regproc", 24), ("text", 25), ("oid", 26), ("tid", 27), ("xid", 28), ("cid", 29), ("json", 114),
    ("xml", 142), ("point", 600), ("float4", 700), ("float8", 701), ("money", 790), ("macaddr", 829),
    ("inet", 869), ("cidr", 650), ("bpchar", 1042), ("varchar", 1043), ("date", 1082), ("time", 1083),
    ("timestamp", 1114), ("timestamptz", 1184), ("interval", 1186), ("timetz", 1266), ("bit", 1560),
    ("varbit", 1562), ("numeric", 1700), ("regtype", 2206), ("uuid", 2950), ("jsonb", 3802),
]
_PSEUDO_TYPES = [("record", 2249), ("void", 2278), ("unknown", 705)]
# Arrays of the common ones, so a driver binding an array parameter finds its type.
_ARRAY_TYPES = [
    ("_bool", 1000, 16), ("_int2", 1005, 21), ("_int4", 1007, 23), ("_text", 1009, 25), ("_varchar", 1015, 1043),
    ("_int8", 1016, 20), ("_float4", 1021, 700), ("_float8", 1022, 701), ("_timestamp", 1115, 1114),
    ("_date", 1182, 1082), ("_timestamptz", 1185, 1184), ("_numeric", 1231, 1700), ("_uuid", 2951, 2950),
]


def _type_records() -> list[dict]:
    """Every type as the columns any version of the type-loading query selects. Npgsql 4 (Power BI) reads
    (nspname, typname, oid, typrelid, typbasetype, type, elemoid, ord); Npgsql 5 and later read
    (nspname, oid, typname, typtype, typnotnull, elemtypoid), where an element type is NULL when there is none."""
    def record(name, oid, kind, elem=None):
        return {"nspname": "pg_catalog", "typname": name, "oid": str(oid), "typrelid": "0", "typbasetype": "0",
                "type": kind, "typtype": kind, "typnotnull": "f", "elemoid": str(elem or 0),
                "elemtypoid": None if elem is None else str(elem), "ord": "3" if kind == "a" else "0"}

    return ([record(n, o, "b") for n, o in _BASE_TYPES] + [record(n, o, "p") for n, o in _PSEUDO_TYPES]
            + [record(n, o, "a", e) for n, o, e in _ARRAY_TYPES])


def _selected_columns(sql: str) -> list[str]:
    """The output names of a query's outermost SELECT list: each item's alias, else its column name."""
    body = sql[sql.upper().index("SELECT") + 6:]
    items, depth, start, i = [], 0, 0, 0
    upper = body.upper()
    while i < len(body):
        c = body[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0 and c == ",":
            items.append(body[start:i])
            start = i + 1
        elif depth == 0 and upper.startswith(" FROM ", i):
            break
        i += 1
    items.append(body[start:i])
    names = []
    for item in items:
        words = item.split()
        alias = words[-1] if len(words) > 1 and words[-2].upper() == "AS" else item.strip()
        names.append(alias.split(".")[-1].strip('"').lower())
    return names


def catalog_answer(sql: str) -> tuple[list[str], list[tuple]] | None:
    """The answer to a Postgres-only query a driver sends (Npgsql's type loading, and the advisory-lock release in
    its session reset), as (columns, text rows), or None for any other query.

    Matched on Npgsql's marker comments and, failing those, on the tables only these queries read.
    """
    s = " ".join(line.split("--", 1)[0] for line in sql.splitlines())     # line comments out, then one line
    s = " ".join(s.split())
    if "Load all supported types" in s or ("typreceive" in s and "pg_range" in s):
        columns = _selected_columns(s)
        return columns, [tuple(r.get(c) for c in columns) for r in _type_records()]
    if "Load field definitions for (free-standing) composite types" in s or (
            "attisdropped" in s and "relkind='c'" in s.replace(" ", "")):
        return ["oid", "attname", "atttypid"], []
    if "Load enum fields" in s or ("FROM pg_enum" in s and "enumlabel" in s):
        return ["oid", "enumlabel"], []
    if s.rstrip(";").lower() == "select pg_advisory_unlock_all()":     # part of Npgsql's session reset
        return ["pg_advisory_unlock_all"], [("",)]
    return None
