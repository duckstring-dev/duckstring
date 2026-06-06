"""RippleExecutor — runs a Pond's Ripple functions in a thread pool.

Reuses the ripple-loading / execution / parquet-export helpers from the existing pond worker (these
move here permanently in the cleanup phase). Each Duck has one executor bound to its Pond's deployed
source. Execution is otherwise opaque to :class:`~duckstring.duck.core.DuckCore`, which only needs
"launch this Ripple" and "tell me when it finished".
"""

from __future__ import annotations

import importlib
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..catchment.pond_worker import _export_parquet, _load_ripple_func, _run_ripple
from ..catchment.registry import pond_registry_path


def load_topology(source_dir: Path) -> dict[str, list[str]]:
    """Build the intra-Pond ``{ripple_name: [parent_names]}`` graph by importing the deployed
    ``src/pond.py`` and reading the registered ripples (the Duck owns its own code)."""
    from ..core import collect_ripples

    src = str(source_dir / "src")
    before = set(sys.modules.keys())
    sys.path.insert(0, src)
    try:
        sys.modules.pop("pond", None)
        importlib.invalidate_caches()
        importlib.import_module("pond")
        ripples = collect_ripples()
    finally:
        if src in sys.path:
            sys.path.remove(src)
        for key in list(sys.modules):
            if key not in before:
                sys.modules.pop(key, None)
    func_to_name = {r["func"]: r["name"] for r in ripples}
    return {
        r["name"]: [func_to_name[p] for p in r["parents"] if p in func_to_name]
        for r in ripples
    }


class RippleExecutor:
    def __init__(self, pond_name: str, version: str, source_path: str, root: Path, max_workers: int = 8):
        self.pond_name = pond_name
        self.version = version
        self.source_path = source_path
        self.root = root
        self.registry_path = pond_registry_path(root, pond_name)
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self._pool = ThreadPoolExecutor(max_workers=max_workers)

    def submit(self, ripple_name: str, on_done, on_error):
        """Load and run ``ripple_name``; call ``on_done(name)`` on success, ``on_error(name, exc)`` on
        failure. Both callbacks fire on a pool thread."""

        def _task():
            func = _load_ripple_func(self.source_path, str(self.root), ripple_name)
            _run_ripple(func, self.pond_name, self.version, str(self.registry_path), str(self.root))

        fut = self._pool.submit(_task)

        def _cb(f):
            exc = f.exception()
            if exc:
                on_error(ripple_name, exc)
            else:
                on_done(ripple_name)

        fut.add_done_callback(_cb)
        return fut

    def export(self) -> None:
        """Export the Pond's tables to Parquet for cross-Pond consumption (atomic tmp+replace)."""
        _export_parquet(self.registry_path)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)
