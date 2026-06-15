import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA_DIR = Path(__file__).parent / "schema"


def ensure_identity(con: sqlite3.Connection, name: str | None = None) -> None:
    """Mint this Catchment's stable id on first start (never changes), and set/refresh its optional
    display name. Call after migrate()."""
    row = con.execute("SELECT value FROM catchment_meta WHERE key = 'id'").fetchone()
    if row is None:
        con.execute("INSERT INTO catchment_meta (key, value) VALUES ('id', ?)", (str(uuid.uuid4()),))
    if name:
        con.execute(
            "INSERT INTO catchment_meta (key, value) VALUES ('name', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (name,),
        )
    con.commit()


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path, check_same_thread=False)
    if path != Path(":memory:") and path.exists():
        path.chmod(0o600)  # may hold duct credentials (auth headers for upstream Catchments)
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA busy_timeout = 5000")  # queue on a locked DB (up to 5 s) instead of erroring
    return con


def migrate(con: sqlite3.Connection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)
    con.commit()

    applied = {row[0] for row in con.execute("SELECT version FROM schema_migrations")}

    sql_files = sorted(_SCHEMA_DIR.glob("*.sql"))
    if not sql_files:
        raise RuntimeError(f"No schema migrations found at {_SCHEMA_DIR} — broken duckstring installation?")
    for sql_file in sql_files:
        version = int(sql_file.stem.split("_")[0])
        if version in applied:
            continue
        con.executescript(sql_file.read_text())
        con.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
