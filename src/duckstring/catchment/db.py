import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA_DIR = Path(__file__).parent / "schema"


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path, check_same_thread=False)
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

    for sql_file in sorted(_SCHEMA_DIR.glob("*.sql")):
        version = int(sql_file.stem.split("_")[0])
        if version in applied:
            continue
        con.executescript(sql_file.read_text())
        con.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
