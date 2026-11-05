"""The pg wire against Npgsql, the driver Power BI's PostgreSQL connector uses (Desktop and the on-premises gateway
bundle Npgsql 4.0.17). ``tests/npgsql`` is a small .NET client that connects, reads every common type in binary,
binds parameters, prepares, reuses a pooled connection, and replays a logged Power BI session (navigator, keys,
preview). It's built per Npgsql version and run against a live server here, plain and over TLS.

Runs when ``DUCKSTRING_TEST_NPGSQL`` lists the Npgsql versions to test (comma separated, such as
``4.0.17,10.0.3``) and the .NET SDK (``dotnet``) is installed; CI runs it in the ``npgsql`` job.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest
from trickle.io import first_row

from duckstring.catchment.db import connect, migrate
from duckstring.catchment.driver import Driver
from duckstring.catchment.launcher import NoopLauncher
from duckstring.catchment.pg_wire import PgWireServer, tls_context
from duckstring.catchment.registry import pond_data_dir
from duckstring.catchment.routes.deploy import _register

pytestmark = [
    pytest.mark.timeout(600),
    pytest.mark.skipif(not os.environ.get("DUCKSTRING_TEST_NPGSQL"), reason="set DUCKSTRING_TEST_NPGSQL to run"),
    pytest.mark.skipif(shutil.which("dotnet") is None, reason="needs the .NET SDK (dotnet)"),
]

_PROBE = Path(__file__).parent / "npgsql"
_VERSIONS = [v.strip() for v in os.environ.get("DUCKSTRING_TEST_NPGSQL", "4.0.17").split(",") if v.strip()]
_KEY = "probe-key"

# The tables the probe reads: sales.revenue (ids 0..6) and sales.kinds (three rows of mixed types).
_TABLES = {
    "revenue": "SELECT range AS id FROM range(7)",
    "kinds": """SELECT i::INTEGER AS i, i::BIGINT * 1000000000 AS b, i * 1.25 AS d, (i * 3.5)::DECIMAL(12,2) AS amount,
                'name ' || i AS s, DATE '2026-01-01' + i::INTEGER AS day,
                TIMESTAMP '2026-01-01 00:00:00' + INTERVAL (i) HOUR AS ts, i % 2 = 0 AS flag FROM range(3) t(i)""",
}


@pytest.fixture(scope="module", params=_VERSIONS)
def probe(request, tmp_path_factory) -> tuple[str, Path]:
    """The probe built against one Npgsql version: (version, path to its dll)."""
    version = request.param
    work = tmp_path_factory.mktemp(f"npgsql-{version}")
    for f in ("NpgsqlProbe.csproj", "Program.cs"):
        shutil.copy(_PROBE / f, work / f)
    out = work / "out"
    build = subprocess.run(["dotnet", "build", "-c", "Release", f"-p:NpgsqlVersion={version}", "-o", str(out)],
                           cwd=work, capture_output=True, text=True, timeout=500)
    if build.returncode != 0:
        if "NU1301" in build.stdout or "Unable to load the service index" in build.stdout:
            pytest.skip("NuGet unreachable")
        pytest.fail(f"building the Npgsql {version} probe failed:\n{build.stdout[-4000:]}")
    return version, out / "NpgsqlProbe.dll"


def _server(root: Path, *, tls=None) -> PgWireServer:
    db = connect(root / "duck.db")
    migrate(db)
    _register(db, "sales", "1.0.0", "outlet", "ponds/sales/1.0.0",
              {"sources": {}, "immediate_retries": 0, "source_retries": 0, "kind": "outlet",
               "serve_tables": list(_TABLES)}, [{"func": "f", "name": "r", "parents": []}])
    pv = first_row(db.execute("SELECT id FROM pond_version"))[0]
    for table in _TABLES:
        db.execute('INSERT INTO pond_version_schema (pond_version_id, "table", "column", type) VALUES (?, ?, ?, ?)',
                   (pv, table, "id", "BIGINT"))
    db.commit()
    data_dir = pond_data_dir(root, "sales", 1, None)
    data_dir.mkdir()
    con = duckdb.connect()
    for table, query in _TABLES.items():
        with data_dir.copy_to(f"{table}.parquet") as uri:
            con.execute(f"COPY ({query}) TO '{uri}' (FORMAT PARQUET)")
    con.close()
    server = PgWireServer(Driver(db, root, "http://x", NoopLauncher()), api_key=_KEY, host="127.0.0.1", port=0,
                          tls=tls)
    server.start()
    return server


def _run(dll: Path, connection: str) -> None:
    result = subprocess.run(["dotnet", str(dll), connection], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def _self_signed(directory: Path) -> tuple[Path, Path]:
    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = directory / "server.crt", directory / "server.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


def test_npgsql_reads_types_parameters_and_a_power_bi_session(probe, tmp_path):
    version, dll = probe
    server = _server(tmp_path)
    try:
        _run(dll, f"Host=127.0.0.1;Port={server.port};Username=analyst;Password={_KEY};Database=duckstring;"
                  "SSL Mode=Disable")
    finally:
        server.stop()


def test_npgsql_connects_over_tls(probe, tmp_path):
    version, dll = probe
    server = _server(tmp_path, tls=tls_context(*map(str, _self_signed(tmp_path))))
    # The probe trusts the self-signed certificate: Npgsql 4 validates under Require unless told to trust it,
    # and Npgsql 6 and later only validate under VerifyCA/VerifyFull.
    trust = "Trust Server Certificate=true;" if version.startswith("4.") else ""
    try:
        _run(dll, f"Host=localhost;Port={server.port};Username=analyst;Password={_KEY};Database=duckstring;"
                  f"SSL Mode=Require;{trust}")
    finally:
        server.stop()
