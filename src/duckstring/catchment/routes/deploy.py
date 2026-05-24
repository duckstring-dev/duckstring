from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()


def _read_toml(text: str) -> dict:
    if sys.version_info >= (3, 11):
        import tomllib
        return tomllib.loads(text)
    import tomli
    return tomli.loads(text)


def _parse_version(s: str) -> tuple[int, str, bool]:
    """Parse "1.2.3" or "1.2.3?" → (major, min_version, required)."""
    required = not s.endswith("?")
    ver = s.rstrip("?")
    major = int(ver.split(".")[0])
    return major, ver, required


def _discover_ripples(source_dir: Path) -> list[dict]:
    """Import src/pond.py from source_dir and collect registered ripples.

    Returns an empty list if the file is missing or fails to import — the
    catchment can still store the source files; ripple registration can be
    retried or handled at execution time.
    """
    from duckstring.core import collect_ripples

    pond_py = source_dir / "src" / "pond.py"
    if not pond_py.exists():
        return []

    src_path = str(source_dir / "src")
    before = set(sys.modules.keys())
    sys.path.insert(0, src_path)
    try:
        importlib.invalidate_caches()
        sys.modules.pop("pond", None)
        importlib.import_module("pond")
        return collect_ripples()
    except Exception:
        collect_ripples()  # drain any partial registrations
        return []
    finally:
        if src_path in sys.path:
            sys.path.remove(src_path)
        for key in list(sys.modules.keys()):
            if key not in before:
                sys.modules.pop(key, None)


def _register(
    db,
    name: str,
    version: str,
    kind: str,
    source_path: str,
    sources: dict,
    ripples: list[dict],
    immediate_retries: int = 0,
    source_retries: int = 0,
) -> None:
    major = int(version.split(".")[0])
    with db:
        db.execute("INSERT OR IGNORE INTO pond (name, kind) VALUES (?, ?)", (name, kind))
        (pond_id,) = db.execute("SELECT id FROM pond WHERE name = ?", (name,)).fetchone()

        # Deactivate any other active version in this major line.
        db.execute(
            "UPDATE pond_version SET is_active = 0 WHERE pond_id = ? AND major = ? AND is_active = 1",
            (pond_id, major),
        )

        existing = db.execute(
            "SELECT id FROM pond_version WHERE pond_id = ? AND version = ?",
            (pond_id, version),
        ).fetchone()

        if existing:
            # Re-deploy: clear stale state and re-activate the existing row.
            version_id = existing[0]
            ripple_ids = [r[0] for r in db.execute(
                "SELECT id FROM ripple WHERE pond_version_id = ?", (version_id,)
            ).fetchall()]
            if ripple_ids:
                marks = ",".join("?" * len(ripple_ids))
                db.execute(f"DELETE FROM ripple_to_ripple WHERE sink_id IN ({marks}) OR source_id IN ({marks})", ripple_ids * 2)
                db.execute("DELETE FROM ripple WHERE pond_version_id = ?", (version_id,))
            db.execute("DELETE FROM pond_to_pond WHERE pond_version_id = ?", (version_id,))
            db.execute("DELETE FROM demand WHERE pond_version_id = ?", (version_id,))
            db.execute("DELETE FROM stop WHERE pond_version_id = ?", (version_id,))
            db.execute(
                "UPDATE pond_version SET is_active = 1, is_stopped = 1, source_path = ?, "
                "deployed_at = datetime('now'), immediate_retries = ?, source_retries = ? WHERE id = ?",
                (source_path, immediate_retries, source_retries, version_id),
            )
        else:
            db.execute(
                "INSERT INTO pond_version "
                "(pond_id, version, major, is_active, source_path, immediate_retries, source_retries) "
                "VALUES (?, ?, ?, 1, ?, ?, ?)",
                (pond_id, version, major, source_path, immediate_retries, source_retries),
            )
            (version_id,) = db.execute(
                "SELECT id FROM pond_version WHERE pond_id = ? AND version = ?", (pond_id, version)
            ).fetchone()

        for src_name, ver_str in sources.items():
            src_major, src_min, src_required = _parse_version(ver_str)
            db.execute("INSERT OR IGNORE INTO pond (name, kind) VALUES (?, 'pond')", (src_name,))
            (src_pond_id,) = db.execute("SELECT id FROM pond WHERE name = ?", (src_name,)).fetchone()
            db.execute(
                """INSERT OR REPLACE INTO pond_to_pond
                   (pond_version_id, source_pond_id, source_major, min_version, required)
                   VALUES (?, ?, ?, ?, ?)""",
                (version_id, src_pond_id, src_major, src_min, int(src_required)),
            )

        func_to_name = {r["func"]: r["name"] for r in ripples}
        name_to_id: dict[str, int] = {}
        for r in ripples:
            db.execute(
                "INSERT OR IGNORE INTO ripple (pond_version_id, name) VALUES (?, ?)",
                (version_id, r["name"]),
            )
            (ripple_id,) = db.execute(
                "SELECT id FROM ripple WHERE pond_version_id = ? AND name = ?",
                (version_id, r["name"]),
            ).fetchone()
            name_to_id[r["name"]] = ripple_id

        for r in ripples:
            sink_id = name_to_id[r["name"]]
            for parent_func in r["parents"]:
                parent_name = func_to_name.get(parent_func, getattr(parent_func, "__name__", None))
                if parent_name and parent_name in name_to_id:
                    db.execute(
                        "INSERT OR IGNORE INTO ripple_to_ripple (sink_id, source_id) VALUES (?, ?)",
                        (sink_id, name_to_id[parent_name]),
                    )

        from ..dag import assert_no_cycles
        assert_no_cycles(db)


class _GitBody(BaseModel):
    name: str
    version: str
    type: str = "pond"
    git_ref: str
    repo_url: str


@router.post("/deploy")
async def deploy(request: Request):
    db = request.app.state.db
    root: Path = request.app.state.root
    ct = request.headers.get("content-type", "")

    if "multipart/form-data" in ct:
        form = await request.form()
        name = form["name"]
        version = form["version"]
        kind = form.get("type", "pond")
        archive_bytes = await form["pond"].read()

        dest = root / "ponds" / name / version
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)

        try:
            with zipfile.ZipFile(dest / "_upload.zip", "w") as _:
                pass  # placeholder; extract directly below
            import io
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
                zf.extractall(dest)
        except zipfile.BadZipFile as exc:
            shutil.rmtree(dest, ignore_errors=True)
            raise HTTPException(status_code=422, detail="Uploaded file is not a valid zip archive") from exc

        toml_path = dest / "pond.toml"
        sources: dict = {}
        immediate_retries = 0
        source_retries = 0
        if toml_path.exists():
            info = _read_toml(toml_path.read_text(encoding="utf-8"))
            sources = info.get("sources", {})
            immediate_retries = info.get("pond", {}).get("immediate_retries", 0)
            source_retries = info.get("pond", {}).get("source_retries", 0)

    else:
        body = _GitBody(**(await request.json()))
        name, version, kind = body.name, body.version, body.type

        dest = root / "ponds" / name / version
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)

        try:
            subprocess.run(
                ["git", "clone", body.repo_url, str(dest)],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(dest), "checkout", body.git_ref],
                check=True, capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            shutil.rmtree(dest, ignore_errors=True)
            raise HTTPException(status_code=422, detail=f"git clone failed: {exc.stderr.decode()}") from exc

        toml_path = dest / "pond.toml"
        sources: dict = {}
        immediate_retries = 0
        source_retries = 0
        if toml_path.exists():
            info = _read_toml(toml_path.read_text(encoding="utf-8"))
            sources = info.get("sources", {})
            immediate_retries = info.get("pond", {}).get("immediate_retries", 0)
            source_retries = info.get("pond", {}).get("source_retries", 0)

    ripples = _discover_ripples(dest)
    source_path = f"ponds/{name}/{version}"
    try:
        _register(db, name, version, kind, source_path, sources, ripples, immediate_retries, source_retries)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {"ok": True}
