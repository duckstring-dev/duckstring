"""Which files of a Pond project are deployed: everything except what ``.pondignore`` excludes.

``.pondignore`` sits at the Pond's root and uses ``.gitignore`` syntax. Without one, :data:`DEFAULT_PATTERNS`
apply: local test data (``puddles/``), secrets (``.env``), hidden directories (``.git/``, ``.venv/``), and
caches and build output. A ``.pondignore`` replaces the defaults entirely; ``duckstring pond init`` writes
one containing them, so they're visible and editable.

Used by ``duckstring pond deploy`` (which files go into the upload) and by the Catchment's ``--git`` deploy
(which files are kept from the clone), so both paths deploy the same set.
"""

from __future__ import annotations

import shutil
from pathlib import Path

IGNORE_FILE = ".pondignore"

DEFAULT_PATTERNS = """\
# Files matching these patterns are not deployed by `duckstring pond deploy`.
# The syntax is the same as .gitignore. This file replaces the built-in defaults,
# which are what it contains when `duckstring pond init` creates it.

# Local test data (duckstring pond hydrate / run)
puddles/

# Secrets and local environment
.env
.env.*

# Hidden directories: version control, virtual environments, tool caches
.*/

# Python caches and build output
__pycache__/
*.py[co]
*.egg-info/
dist/
build/
node_modules/
"""


def _spec(root: Path):
    import pathspec

    path = Path(root) / IGNORE_FILE
    text = path.read_text(encoding="utf-8") if path.is_file() else DEFAULT_PATTERNS
    return pathspec.GitIgnoreSpec.from_lines(text.splitlines())


def deployed_files(root: Path) -> list[Path]:
    """The files under ``root`` that a deploy includes, as paths relative to ``root``, sorted."""
    root = Path(root)
    spec = _spec(root)
    out: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(root)
        if not spec.match_file(rel.as_posix()):
            out.append(rel)
    return out


def prune(root: Path) -> None:
    """Delete everything under ``root`` that a deploy excludes (for a checkout the Catchment made itself)."""
    root = Path(root)
    keep = {rel.as_posix() for rel in deployed_files(root)}
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink() or path.is_file():
            if rel not in keep:
                path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    git_dir = root / ".git"
    if git_dir.exists():  # usually already emptied above; make sure no repository metadata is left behind
        shutil.rmtree(git_dir, ignore_errors=True)
