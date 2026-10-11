"""The Flock — duckstring's over-envelope compute tier.

Work that fits the Duck runs in the Duck; a ``pond.trickle(...)`` terminal whose comprehensive
recompute would exceed the Duck's envelope is dispatched to another engine, **the Flock**, which
runs the heavy read side (scan/join/filter/project/aggregate, or a ``.sql()`` query); the Duck keeps
the merge/diff/publish semantics locally. What is offered is a :class:`trickle.ComprehensiveJob`
(the recompute as one self-contained DuckDB query plus its sources). The engine is **pluggable**
and chosen in this order unless one is named (plans/flock-coverage.md):

1. ``motherduck``: DuckDB in MotherDuck. the job's own SQL runs unchanged, so everything SQL is
   eligible and results match by construction.
2. ``starrocks``: a distributed SQL engine; the job is translated (sqlglot) and only constructs
   whose translation was measured equal are sent.
3. ``athena``: likewise, on Trino.

The builder hook, the per-Pond posture, and this policy layer are all engine-agnostic.

**No metering here.** OSS dispatches and runs; it never bills. An engine's *cloud* deployment
accounts for its own usage out of band (for Athena: per-tenant workgroup → CloudWatch
bytes-scanned). The engine protocol has no cost surface — dispatch returns a result relation or
``None``, nothing more.

Posture ladder (a **Pond-level** setting — ``pond.toml [flock] mode`` coalesced with an operator
override, carried to the Duck on each ``begin_run`` job (see :func:`job_settings`); inert without a
configured engine, so the same code runs anywhere — plans/cloud-config.md):

- **always**: every eligible comprehensive recompute runs on the Flock (the known-heavy declaration).
- **upgrade**: dispatch up front when *clearly* over the envelope, else run the local recompute
  bounded+materialised and fail up on OOM.
- **off** — never.

Only a run that recomputes wholesale anyway is offered (a bootstrap, ``ivm=False``, a coverage miss, a
source over its ``p`` threshold, or a ``.sql()`` result); an incremental run is small by construction and
always runs in the Duck.

Config (env names; the Catchment sends them on each ``begin_run`` job, which works for every launcher,
including Fargate and EC2 Ducks that inherit no environment from it, and the Duck overlays them on its
own environment for that run):

- ``DUCKSTRING_FLOCK_ENGINE``: engine name (``motherduck``, ``motherduck/<profile>``, ``starrocks``,
  ``athena``, or a ``module:Class`` spec); unset ⇒ the first configured engine in the order above; an
  unknown or unconfigured one ⇒ off.
- ``DUCKSTRING_FLOCK_MODE``    — the Pond's resolved posture (off|upgrade|always; set by the Duck from
  its effective config — plans/cloud-config.md; a bare env default of ``off`` otherwise).
- ``DUCKSTRING_FLOCK_OOM_POLICY``— ``fail_up`` (default; offload on OOM) | ``fail`` (hard cap).
- ``DUCKSTRING_FLOCK_MIN_ROWS``— explicit envelope override (rows summed over source leaves).
- ``DUCKSTRING_FLOCK_FORCE=1`` — force ``always`` (testing/demo).
- ``DUCKSTRING_MEMORY_LIMIT`` — the Duck's DuckDB memory cap (the OOM fail-up depends on it); the
  "clearly over the envelope" row threshold is DERIVED from it (no abstract size preset — the box's
  real memory is the envelope). Absent ⇒ a conservative default.
- engine-specific config lives under the engine's own namespace (Athena:
  ``DUCKSTRING_FLOCK_ATHENA_{WORKGROUP,DATABASE,SCRATCH,REGION}``), and an engine's credentials are the
  secret names it declares in ``SECRETS`` (see :func:`engine_secrets`).
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from typing import Protocol

log = logging.getLogger("duckstring.flock")

FLOCK_MODES = (None, "always", "upgrade", "off")

# The comprehensive-recompute row envelope (sum over source leaves) is derived from the Duck's real
# memory cap: ~this many rows per GiB the box can absorb before a local recompute is clearly doomed.
# Deliberately conservative; the sidecar-stats-aware estimate is the E1-proper follow-up. The OOM
# fail-up (the memory cap) is the real safety net — this only pre-empts an obviously-doomed local try.
_ROWS_PER_GIB = 500_000
_DEFAULT_MIN_ROWS = 4_000_000  # when the memory cap is unknown (a stock local Duck)

# Process-lifetime dispatch counters (this module runs inside the Duck). Shipped on the Duck's
# run_completed event so a *degrading* Flock is visible from the Catchment (/metrics, /api/status) —
# the fallback contract means a failed dispatch still produces a correct run, which otherwise looks
# identical to a working Flock while quietly paying full local compute every time.
_stats = {"dispatched": 0, "failed": 0, "last_error": None}


def stats() -> dict | None:
    """A peek at this Duck's dispatch counters — ``None`` while nothing was ever attempted."""
    if not _stats["dispatched"] and not _stats["failed"]:
        return None
    return dict(_stats)


def take_stats() -> dict | None:
    """The dispatches since the last call, **and reset** — the per-Ripple-Run delta the Duck ships on its
    ripple event, which the Catchment accumulates.

    A delta rather than a process-lifetime snapshot, and carried by the *ripple* event rather than run
    completion, because neither the process nor the run is a reliable carrier: a Duck killed mid-run
    (OOM, a reaped box) is respawned and resumes from its ledger, so the process that dispatched is often
    NOT the one that completes the Pond Run — a snapshot read at completion would silently be zero."""
    if not _stats["dispatched"] and not _stats["failed"]:
        return None
    out = dict(_stats)
    _stats.update({"dispatched": 0, "failed": 0, "last_error": None})
    return out


def _record_dispatch(engine, result, error: str | None = None) -> None:
    if result is None:
        _stats["failed"] += 1
        _stats["last_error"] = (error or getattr(engine, "last_error", None)
                                or "dispatch failed (see the Duck log)")
    else:
        _stats["dispatched"] += 1
        _stats["last_error"] = None


def _qi(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def conform(job, result):
    """Force an engine's result to be **exactly what DuckDB would have produced**, or reject it.

    DuckDB is the authority: dispatching decides *where* the work runs, never *what* gets published. An
    engine that returns the same rows with a different column order, or ``DECIMAL(36,2)`` where DuckDB
    says ``DECIMAL(35,2)``, has changed the output — and since the published schema is the version
    contract, that difference can fail a Pond and (before the widening rule) wedge it permanently.

    The oracle is free: ``job.schema()`` is DuckDB's own answer for this job, obtained by binding the
    local plan without executing it. So:

    - a column set that doesn't match the oracle → **reject** (return None → local compute). Not
      something to paper over: it means the engine computed a different query.
    - otherwise project in the oracle's column ORDER with an explicit CAST per column, so the types are
      DuckDB's by construction rather than by coincidence.

    Returns the conformed relation, or ``None`` to fall back."""
    if result is None:
        return None
    try:
        oracle = job.schema()
        got = set(result.columns)
        if got != set(oracle):
            missing, extra = sorted(set(oracle) - got), sorted(got - set(oracle))
            log.warning("flock: engine result does not match the DuckDB oracle (missing=%s extra=%s) "
                        "— running local", missing, extra)
            return None
        projection = ", ".join(f"CAST({_qi(c)} AS {t}) AS {_qi(c)}" for c, t in oracle.items())
        return result.project(projection)
    except Exception as exc:  # noqa: BLE001 — conformance must never fail a Run; local compute is correct
        log.warning("flock: could not conform the engine result (%s: %s) — running local",
                    type(exc).__name__, str(exc)[:200])
        return None


def _dispatch(engine, job, env=None):
    """Dispatch + record. The engine protocol says ``dispatch`` returns ``None`` on an engine-side
    failure, but the fallback contract (the Flock is NEVER load-bearing) must not depend on every engine
    remembering to catch — an engine that raises would fail the Pond Run instead of degrading. So the
    catch-all lives here too."""
    try:
        result = engine.dispatch(job)
        error = None
    except Exception as exc:  # noqa: BLE001 — the fallback contract: degrade to local, never fail the run
        log.warning("flock: engine raised (%s: %s) — running local", type(exc).__name__, str(exc)[:300])
        result, error = None, f"{type(exc).__name__}: {str(exc)[:300]}"
    if result is not None:
        # Fail CLOSED: a result that isn't what DuckDB would have produced is discarded, turning a wrong
        # answer into a slower one. The counters record it as a failed dispatch, so a Flock that is
        # quietly non-conformant shows up on /metrics instead of in your published data.
        conformed = conform(job, result)
        if conformed is None:
            error = "engine result did not conform to the DuckDB schema"
        elif getattr(engine, "VERIFY", False):
            # An engine that isn't DuckDB: recompute a sample of keys locally before trusting it.
            from .verify import sample_size, verify

            try:
                error = verify(job, conformed, keys=sample_size(env if env is not None else os.environ),
                               accept=getattr(engine, "accept", ()))
            except Exception as exc:  # noqa: BLE001 (an unverifiable result is not used)
                error = f"sampled verification failed to run ({type(exc).__name__}: {str(exc)[:200]})"
            if error is not None:
                log.warning("flock: %s; running local", error)
                conformed = None
        result = conformed
    _record_dispatch(engine, result, error)
    return result


class FlockEngine(Protocol):
    """A big-compute backend behind the Flock tier. One implementation per engine. Each method takes the
    offered :class:`trickle.ComprehensiveJob`. No metering surface: usage is accounted for out of band by
    the engine's cloud deployment."""

    def enabled(self) -> bool: ...
    def eligible(self, job) -> str | None: ...              # None = dispatchable; else the reason
    def estimate_rows(self, job) -> int: ...
    def dispatch(self, job): ...                            # result relation, or None on failure


# The Catchment-side keys the Duck takes from the Pond's effective config rather than the environment.
_POSTURE_KEYS = ("DUCKSTRING_FLOCK_MODE", "DUCKSTRING_FLOCK_ENGINE", "DUCKSTRING_FLOCK_OOM_POLICY",
                 "DUCKSTRING_FLOCK_ACCEPT")
# Built-in engine names, as the module:Class spec each stands for.
_BUILTIN_ENGINES = {
    "motherduck": "duckstring.flock.engines.motherduck:MotherDuckEngine",
    "starrocks": "duckstring.flock.engines.starrocks:StarRocksEngine",
    "athena": "duckstring.flock.engines.athena:AthenaEngine",
}
# With no engine named, the first configured one in this order (plans/flock-coverage.md): DuckDB engines
# first (everything is eligible), then the SQL engines (only what translates provably).
ENGINE_ORDER = ("motherduck", "starrocks", "athena")


def _engine_class(name: str):
    """The class engine ``name`` stands for (a built-in name, optionally ``name/profile``, or a
    ``module:Class`` spec), or ``None`` when it can't be imported."""
    import importlib

    base = name.strip().split("/", 1)[0].lower() if ":" not in name else name.strip()
    spec = _BUILTIN_ENGINES.get(base, base)
    if ":" not in spec:
        return None
    module_name, _, class_name = spec.partition(":")
    try:
        return getattr(importlib.import_module(module_name), class_name)
    except Exception as exc:  # noqa: BLE001
        log.warning("flock: cannot load engine %r (%s)", name, exc)
        return None


def engine_secrets(name: str | None) -> tuple[str, ...]:
    """The credential names engine ``name`` needs: its class's ``SECRETS`` (names in the Catchment's secret
    store, else its environment), or its ``secrets_for(name)`` when the name carries a profile
    (``motherduck/giga``). An engine the Catchment can't import declares none (it may only be importable in
    the Duck's environment); that never fails a run. ``None`` means the default engine, which is resolved
    by :func:`default_engine` first."""
    if not name:
        return ()
    cls = _engine_class(name)
    if cls is None:
        return ()
    secrets_for = getattr(cls, "secrets_for", None)
    return tuple(secrets_for(name) if secrets_for else getattr(cls, "SECRETS", ()))


def default_engine(environ=None, secret=None) -> str | None:
    """The engine used when none is named: the first in :data:`ENGINE_ORDER` that is configured, judged on
    ``environ`` plus its declared secrets (from ``secret(name)``, else ``environ``). ``None`` = off."""
    e = os.environ if environ is None else environ
    for name in ENGINE_ORDER:
        cls = _engine_class(name)
        if cls is None:
            continue
        env = dict(e)
        for key in engine_secrets(name):
            value = (secret(key) if secret is not None else None) or e.get(key)
            if value:
                env[key] = value
        try:
            if cls(env, name=name).enabled():
                return name
        except Exception as exc:  # noqa: BLE001 (a broken engine must not take the others down)
            log.warning("flock: engine %r could not be checked (%s)", name, exc)
    return None


def job_settings(duck_cfg: dict, environ=None, secret=None) -> dict[str, str]:
    """The Flock settings a ``begin_run`` job carries, as an environment mapping the Duck overlays on its
    own for that run (built on the Catchment): the Pond's effective posture from ``duck_cfg``
    (``flock_mode``/``flock_engine``/``oom_policy``/``flock_accept``), the Catchment's other ``DUCKSTRING_FLOCK_*``
    settings, and, only when the Flock is on, the engine's declared secrets, each from the secret store
    (``secret(name)``) else the Catchment's environment. The job travels over the authenticated Duck
    channel, so credentials stay out of task definitions and process listings, and a rotated secret is
    used from the next run."""
    e = os.environ if environ is None else environ
    out = {k: v for k, v in e.items() if k.startswith("DUCKSTRING_FLOCK_") and k not in _POSTURE_KEYS}
    mode = (duck_cfg.get("flock_mode") or "off").lower()
    engine = duck_cfg.get("flock_engine") or e.get("DUCKSTRING_FLOCK_ENGINE")
    if not engine and mode != "off":
        # Decided here, where the secrets are, and pinned on the job, so the Duck runs the same engine.
        engine = default_engine(e, secret)
    out["DUCKSTRING_FLOCK_MODE"] = mode
    out["DUCKSTRING_FLOCK_OOM_POLICY"] = (duck_cfg.get("oom_policy") or "fail_up").lower()
    if duck_cfg.get("flock_accept"):
        out["DUCKSTRING_FLOCK_ACCEPT"] = duck_cfg["flock_accept"]
    if engine:
        out["DUCKSTRING_FLOCK_ENGINE"] = engine
    if mode != "off":
        for name in engine_secrets(engine):
            value = (secret(name) if secret is not None else None) or e.get(name)
            if value:
                out[name] = value
    return out


def get_engine(env=None) -> FlockEngine | None:
    """The configured engine, or ``None`` when the Flock is off (no engine / not enabled).

    ``DUCKSTRING_FLOCK_ENGINE`` is a built-in name (``motherduck``, ``motherduck/<profile>``, ``starrocks``,
    ``athena``) or a ``module:Class`` spec, the open engine seam, mirroring ``DUCKSTRING_DUCK_LAUNCHER``.
    Unset, the first configured engine in :data:`ENGINE_ORDER`. The class is constructed with the env
    mapping (built-ins also take ``name=``) and must satisfy :class:`FlockEngine`. Unlike the launcher seam
    this **degrades rather than raises**: the Flock is never load-bearing (a missing engine just means local
    compute), so an unimportable spec warns and turns the Flock off instead of failing the Pond."""
    e = env if env is not None else os.environ
    name = (e.get("DUCKSTRING_FLOCK_ENGINE") or "").strip() or default_engine(e)
    if not name:
        return None
    cls = _engine_class(name)
    if cls is None:
        if ":" not in name and name.split("/", 1)[0].lower() not in _BUILTIN_ENGINES:
            log.warning("flock: unknown DUCKSTRING_FLOCK_ENGINE=%r; off", name)
        return None
    try:
        engine = cls(e, name=name) if ":" not in name else cls(e)
    except Exception as exc:  # noqa: BLE001 (a bad engine must not fail a Pond Run)
        log.warning("flock: cannot build engine %r (%s); off", name, exc)
        return None
    return engine if engine.enabled() else None


def enabled(env=None) -> bool:
    """Cheap gate for the builder hook — is a Flock engine configured and ready?"""
    return get_engine(env) is not None


def _mem_limit_gib(raw: str | None) -> float | None:
    """Parse a DuckDB memory-limit string (``8GB``/``512MB``/``16GiB``/a bare byte count) → GiB."""
    if not raw or not raw.strip():
        return None
    m = re.match(r"\s*([0-9.]+)\s*([kmgt]i?b|b)?\s*$", raw.lower())
    if not m:
        return None
    factor = {"b": 1 / 2**30, "kb": 1 / 2**20, "kib": 1 / 2**20, "mb": 1 / 2**10, "mib": 1 / 2**10,
              "gb": 1, "gib": 1, "tb": 1024, "tib": 1024}.get(m.group(2) or "b", 1)
    return float(m.group(1)) * factor


def _min_rows(env=None) -> int:
    """The "clearly over the envelope, dispatch up front" row threshold. Explicit override wins; else
    derived from the Duck's real memory cap; else a conservative default (no abstract size preset)."""
    e = env if env is not None else os.environ
    if e.get("DUCKSTRING_FLOCK_MIN_ROWS"):
        return int(e["DUCKSTRING_FLOCK_MIN_ROWS"])
    gib = _mem_limit_gib(e.get("DUCKSTRING_MEMORY_LIMIT"))
    return int(gib * _ROWS_PER_GIB) if gib else _DEFAULT_MIN_ROWS


def _oom_policy(env=None) -> str:
    e = env if env is not None else os.environ
    policy = (e.get("DUCKSTRING_FLOCK_OOM_POLICY") or "fail_up").lower()
    return policy if policy in ("fail_up", "fail") else "fail_up"


def _runtime_mode(env=None) -> str:
    e = env if env is not None else os.environ
    if e.get("DUCKSTRING_FLOCK_FORCE", "0") == "1":
        return "always"
    mode = (e.get("DUCKSTRING_FLOCK_MODE") or "upgrade").lower()
    return mode if mode in ("always", "upgrade", "off") else "upgrade"


def resolve_mode(pond_mode: str | None, env=None) -> str:
    """The effective posture for this run: the Pond's resolved posture (threaded through the Duck)
    wins; absent, the runtime default env."""
    return pond_mode or _runtime_mode(env)


def leaf_rows(job) -> int:
    """The rows a job reads: each source's current row count, from metadata where the host can (the
    sidecar and Parquet footers for a published Source), else a count."""
    ctx = job.ctx
    counter = getattr(ctx, "count_table", None)
    total = 0
    for leaf in job.leaves():
        total += int(counter(leaf.ref)) if counter is not None else (
            ctx.read_table(leaf.ref).aggregate("count(*)").fetchone()[0])
    return total


def _probe_local(job):
    """The local comprehensive recompute **bounded + materialised**: DuckDB is already capped at
    the Duck's memory limit (executor-wide, from ``DUCKSTRING_MEMORY_LIMIT``); land the result in
    a temp table *before any output write*, so "too big" surfaces here as a catchable
    ``OutOfMemoryException`` with zero partial-state risk — the caller fails up to the engine."""
    if job.kind == "rows" and job.builder._is_sql:
        return job.builder._materialise()  # a .sql() result already lands in a temp table
    con = job.ctx.con
    name = f"_ds_flock_probe_{uuid.uuid4().hex[:8]}"
    con.execute(f"CREATE TEMP TABLE {name} AS {job.relation().sql_query()}")
    return con.table(name)


def comprehensive(job, *, pond_mode: str | None, env=None):
    """Decide + run an offered comprehensive recompute (a :class:`trickle.ComprehensiveJob`) on the
    Flock. Returns the job's result relation, or ``None`` (run local, the common case). ``pond_mode`` is
    the Pond's resolved posture. ``env`` is the run's Flock settings (the job's, over the Duck's
    environment); ``None`` reads the process environment."""
    engine = get_engine(env)
    if engine is None:
        return None
    mode = resolve_mode(pond_mode, env)
    if mode == "off":
        return None
    # The engine decides what it can be trusted with: both what it can express and which expressions it
    # is known to evaluate the way DuckDB does.
    reason = engine.eligible(job)
    if reason is not None:
        log.info("flock: not dispatching (%s)", reason)
        return None
    if mode == "always":
        return _dispatch(engine, job, env)
    # upgrade: dispatch when clearly over, else local + OOM fail-up.
    if engine.estimate_rows(job) >= _min_rows(env):
        return _dispatch(engine, job, env)
    try:
        return _probe_local(job)
    except Exception as exc:
        import duckdb

        if not isinstance(exc, duckdb.OutOfMemoryException):
            raise
        # oom_policy=fail is the hard cap: surface the OOM as a Pond failure, don't offload.
        if _oom_policy(env) == "fail":
            log.warning("flock: local comprehensive hit the memory limit and oom_policy=fail — not offloading")
            raise
        log.warning("flock: local comprehensive hit the memory limit (%s) — failing up", str(exc)[:200])
        remote = _dispatch(engine, job, env)
        if remote is None:  # the engine also failed — surface the real problem, don't loop
            raise
        return remote
