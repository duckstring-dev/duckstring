"""Measurement shims for Duck processes, each enabled by its own environment variable (a directory; one file per
process):

- DS_PROF_LOG, the step profiler: wraps every function in the Trickle engine, the data plane, the Pond handle and
  the executor, and logs each call slower than DS_PROF_MIN_MS (default 5) with its depth, so a Ripple's time
  splits into its steps. It costs time, so it is kept out of timed runs.
- DS_MEM_LOG, the memory sampler: every DS_MEM_INTERVAL seconds (default 0.25), a Duck's resident memory and what
  its DuckDB holds (duckdb_memory() by tag, its spill, its memory_limit), plus an event each time the Duck
  opens its registry or sets its memory limit. Cheap enough for timed runs. Written as JSON lines.
"""

import functools
import importlib.abc
import importlib.machinery
import inspect
import os
import sys
import threading
import time

LOG_DIR = os.environ.get("DS_PROF_LOG")
MEM_DIR = os.environ.get("DS_MEM_LOG")
MODULES = ("trickle.io", "trickle.builder", "duckstring.dataplane", "duckstring.core", "duckstring.duck.executor",
           "duckstring.duck.core")
SKIP = {"_q", "_qualify", "unique_name", "part_name", "part_f", "first_row", "_alias_for", "_resolve_col"}

if LOG_DIR:
    MIN = float(os.environ.get("DS_PROF_MIN_MS", "5")) / 1000
    _local = threading.local()
    _lock = threading.Lock()
    _out = None
    _wrapped: dict[int, object] = {}

    def _write(line: str) -> None:
        global _out
        with _lock:
            if _out is None:
                os.makedirs(LOG_DIR, exist_ok=True)
                _out = open(os.path.join(LOG_DIR, f"{os.getpid()}.tsv"), "a", buffering=1)
                argv = " ".join(a if not a.startswith("--token=") else "--token=***" for a in sys.argv)
                _out.write(f"#argv\t{argv}\n")  # without the Duck's token
            _out.write(line)

    def _wrap(fn, qual):
        @functools.wraps(fn)
        def inner(*a, **k):
            depth = getattr(_local, "depth", 0)
            _local.depth = depth + 1
            t = time.perf_counter()
            try:
                return fn(*a, **k)
            finally:
                dt = time.perf_counter() - t
                _local.depth = depth
                if dt >= MIN:
                    _write(f"{time.time():.3f}\t{threading.current_thread().name}\t{depth}\t{qual}\t{dt * 1000:.1f}\n")
        inner.__ds_prof__ = True
        return inner

    PLANS = os.environ.get("DS_PROF_PLANS")  # a Pond name: save EXPLAIN ANALYZE of its join queries

    def _plan(con, sql, label) -> None:
        try:
            t = time.perf_counter()
            text = "\n".join(r[1] for r in con.execute("EXPLAIN ANALYZE " + sql).fetchall())
            dt = time.perf_counter() - t
            _write(f"{time.time():.3f}\tplan\t0\tPLAN.{label}\t{dt * 1000:.1f}\n")
            with open(os.path.join(LOG_DIR, f"plan_{os.getpid()}_{time.time():.3f}_{label}.txt"), "w") as fh:
                fh.write(sql + "\n\n" + text)
        except Exception as e:  # noqa: BLE001
            _write(f"{time.time():.3f}\tplan\t0\tPLANERR.{label}:{e!r:.200}\t0\n")

    def _planning(qual, fn):
        if not PLANS or PLANS not in " ".join(sys.argv):
            return fn
        if qual == "io.apply_zset":
            def hook(con, name, zset, *a, **k):
                _plan(con, zset.sql_query(), f"zset_{name}")
                return fn(con, name, zset, *a, **k)
        elif qual == "TrickleBuilder._scratch_table":
            def hook(self, view, *a, **k):
                _plan(self.ctx.con, f'SELECT * FROM "{view}"', "scratch")
                return fn(self, view, *a, **k)
        else:
            return fn
        return functools.wraps(fn)(hook)

    _orig_wrap = _wrap

    def _wrap(fn, qual):  # noqa: F811
        return _orig_wrap(_planning(qual, fn), qual)

    def _patch(mod) -> None:
        for name, obj in list(vars(mod).items()):
            if name in SKIP:
                continue
            if inspect.isfunction(obj) and obj.__module__ == mod.__name__ and not getattr(obj, "__ds_prof__", 0):
                w = _wrapped.setdefault(id(obj), _wrap(obj, f"{mod.__name__.split('.')[-1]}.{name}"))
                setattr(mod, name, w)
            elif inspect.isclass(obj) and obj.__module__ == mod.__name__:
                for mname, m in list(vars(obj).items()):
                    if mname.startswith("__") or mname in SKIP:
                        continue
                    if inspect.isfunction(m) and not getattr(m, "__ds_prof__", 0):
                        setattr(obj, mname, _wrap(m, f"{obj.__name__}.{mname}"))

    def _rebind() -> None:
        # names imported with `from x import f` still point at the original: point them at the wrapper
        for mod in list(sys.modules.values()):
            n = getattr(mod, "__name__", "") or ""
            if not (n.startswith("trickle") or n.startswith("duckstring")):
                continue
            for name, obj in list(vars(mod).items()):
                w = _wrapped.get(id(obj))
                if w is not None and w is not obj:
                    setattr(mod, name, w)



if MEM_DIR:
    import json
    import weakref

    _mem_lock = threading.Lock()
    _mem_out = None
    _registry = {"con": None, "cur": None}

    def _mem_write(rec: dict) -> None:
        global _mem_out
        with _mem_lock:
            if _mem_out is None:
                os.makedirs(MEM_DIR, exist_ok=True)
                _mem_out = open(os.path.join(MEM_DIR, f"{os.getpid()}.jsonl"), "a", buffering=1)
                argv = sys.argv  # never written whole: it carries the Duck's token

                def arg(name):
                    return argv[argv.index(name) + 1] if name in argv[:-1] else None

                _mem_out.write(json.dumps({"t": time.time(), "event": "start", "pid": os.getpid(),
                                           "pond": arg("--pond"), "major": arg("--major")}) + "\n")
            _mem_out.write(json.dumps(rec) + "\n")

    try:
        import psutil as _psutil

        _me = _psutil.Process()

        def _rss() -> int | None:
            return _me.memory_info().rss
    except ImportError:
        def _rss() -> int | None:
            try:
                with open("/proc/self/statm") as fh:
                    return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
            except OSError:
                return None

    def _peak_rss() -> int:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak if sys.platform == "darwin" else peak * 1024  # bytes on macOS, KiB on Linux

    def _mem_sample() -> dict:
        rec = {"t": round(time.time(), 3), "rss": _rss(), "rss_peak": _peak_rss()}
        con, cur = _registry["con"], _registry["cur"]
        if cur is not None and (con is None or con() is None):  # the Duck dropped that registry
            try:
                cur.close()
            except Exception:  # noqa: BLE001
                pass
            _registry["cur"] = cur = None
        if cur is not None:
            try:
                tags = cur.execute("SELECT tag, memory_usage_bytes, temporary_storage_bytes FROM duckdb_memory()"
                                   " WHERE memory_usage_bytes > 0 OR temporary_storage_bytes > 0").fetchall()
                rec["db"] = sum(t[1] for t in tags)
                rec["db_spill"] = sum(t[2] for t in tags)
                rec["tags"] = {t[0]: t[1] for t in tags if t[1]}
                rec["limit"] = cur.execute("SELECT current_setting('memory_limit')").fetchall()[0][0]
                rec["temp_files"] = cur.execute("SELECT coalesce(sum(size), 0) FROM duckdb_temporary_files()"
                                                ).fetchall()[0][0]
            except Exception as e:  # noqa: BLE001
                rec["error"] = repr(e)[:200]
        return rec

    def _mem_loop() -> None:
        every = float(os.environ.get("DS_MEM_INTERVAL", "0.25"))
        while True:
            try:
                _mem_write(_mem_sample())
            except Exception:  # noqa: BLE001
                pass
            time.sleep(every)

    def _mem_patch(module) -> None:
        orig_open = module.open_registry

        def open_registry(path, *a, **k):
            con, state = orig_open(path, *a, **k)
            try:  # a cursor of its own: another connection on the same instance, so duckdb_memory() sees it all
                old = _registry["cur"]
                _registry["con"], _registry["cur"] = weakref.ref(con), con.cursor()
                if old is not None:
                    old.close()
            except Exception:  # noqa: BLE001
                pass
            _mem_write({"t": round(time.time(), 3), "event": "open_registry", "path": str(path), "state": state})
            return con, state

        module.open_registry = functools.wraps(orig_open)(open_registry)
        cls = module.RippleExecutor
        orig_set = cls.set_memory_limit

        def set_memory_limit(self, limit, *a, **k):
            ok = orig_set(self, limit, *a, **k)
            _mem_write({"t": round(time.time(), 3), "event": "memory_limit", "limit": limit, "ok": ok})
            return ok

        cls.set_memory_limit = functools.wraps(orig_set)(set_memory_limit)
        threading.Thread(target=_mem_loop, name="ds-mem", daemon=True).start()


if LOG_DIR or MEM_DIR:
    class _Loader(importlib.abc.Loader):
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):  # get_code, get_source, is_package... for runpy (python -m)
            return getattr(self.inner, name)

        def create_module(self, spec):
            return self.inner.create_module(spec)

        def exec_module(self, module):
            self.inner.exec_module(module)
            if MEM_DIR and module.__name__ == "duckstring.duck.executor" and "--pond" in sys.argv:  # a Duck
                _mem_patch(module)
            if LOG_DIR and module.__name__ in MODULES:
                _patch(module)
            if LOG_DIR and module.__name__.startswith(("trickle", "duckstring")):
                _rebind()

    class _Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if not name.startswith(("trickle", "duckstring")):
                return None
            spec = importlib.machinery.PathFinder.find_spec(name, path)
            if spec is not None and spec.loader is not None and not isinstance(spec.loader, _Loader):
                spec.loader = _Loader(spec.loader)
            return spec

    sys.meta_path.insert(0, _Finder())
