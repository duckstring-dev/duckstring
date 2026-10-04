"""The Flock settings ride the begin_run job (plans/flock-motherduck.md, "Getting Flock settings to every
Duck"): the Catchment builds them from the Pond's effective config, its own ``DUCKSTRING_FLOCK_*``
settings and the engine's declared secrets, and the Duck uses them for that run. So a Duck whose launcher
passed it no environment (Fargate, EC2) still dispatches, and a rotated secret is used from the next run."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import duckdb

from duckstring import flock
from duckstring.core import Pond

UTC = timezone.utc
ENGINE = "test_flock_job:RecordingEngine"  # the name pytest imports this module under


class RecordingEngine:
    """A FlockEngine that declares a secret and records the value it was constructed with."""

    SECRETS = ("REC_TOKEN",)
    seen: list = []

    def __init__(self, env):
        self.token = env.get("REC_TOKEN")
        self.last_error = None

    def enabled(self):
        return self.token is not None

    def eligible(self, builder):
        return None

    def estimate_rows(self, builder):
        return 10_000_000

    def dispatch(self, builder, out_pk):
        RecordingEngine.seen.append(self.token)
        con = builder.ctx.con
        name = f"_ds_rec_{uuid.uuid4().hex[:8]}"
        con.execute(f"CREATE TEMP TABLE {name} AS {builder._full_join().sql_query()}")
        return con.table(name)


def _clear_flock_env(monkeypatch):
    import os

    for k in [k for k in os.environ if k.startswith("DUCKSTRING_FLOCK_") or k == "REC_TOKEN"]:
        monkeypatch.delenv(k)


# ─── building the settings (Catchment side) ──────────────────────────────────────


def test_job_settings_carry_the_posture_the_catchments_settings_and_the_secrets():
    environ = {"DUCKSTRING_FLOCK_ATHENA_WORKGROUP": "wg", "DUCKSTRING_FLOCK_MODE": "always",
               "REC_TOKEN": "from-env", "UNRELATED": "x"}
    out = flock.job_settings({"flock_mode": "upgrade", "flock_engine": ENGINE, "oom_policy": "fail"},
                             environ=environ, secret=lambda n: "from-store" if n == "REC_TOKEN" else None)
    assert out == {
        "DUCKSTRING_FLOCK_ATHENA_WORKGROUP": "wg",
        "DUCKSTRING_FLOCK_MODE": "upgrade",  # the Pond's config, not the Catchment's environment
        "DUCKSTRING_FLOCK_OOM_POLICY": "fail",
        "DUCKSTRING_FLOCK_ENGINE": ENGINE,
        "REC_TOKEN": "from-store",  # the secret store wins over the environment
    }
    # No store value: the Catchment's environment supplies it.
    out = flock.job_settings({"flock_mode": "always", "flock_engine": ENGINE}, environ=environ, secret=None)
    assert out["REC_TOKEN"] == "from-env"


def test_no_credentials_are_sent_while_the_flock_is_off():
    out = flock.job_settings({"flock_mode": "off", "flock_engine": ENGINE},
                             environ={}, secret=lambda n: "s3cret")
    assert "REC_TOKEN" not in out and out["DUCKSTRING_FLOCK_MODE"] == "off"


def test_engine_secrets():
    assert flock.engine_secrets(None) == ()  # Athena authenticates with the Duck's IAM role
    assert flock.engine_secrets("athena") == ()
    assert flock.engine_secrets(ENGINE) == ("REC_TOKEN",)
    assert flock.engine_secrets("no_such_module:Engine") == ()  # importable only on the Duck: none


def test_the_begin_run_job_carries_the_flock_settings(tmp_path, monkeypatch):
    from duckstring.catchment.db import connect, migrate
    from duckstring.catchment.driver import Driver
    from duckstring.catchment.launcher import NoopLauncher
    from duckstring.catchment.routes.deploy import _register
    from duckstring.egress import credentials

    _clear_flock_env(monkeypatch)
    monkeypatch.setattr(credentials, "_secret_provider", lambda n: "tok-1" if n == "REC_TOKEN" else None)
    db = connect(tmp_path / "duck.db")
    migrate(db)
    _register(db, "p", "1.0.0", "inlet", "ponds/p/1.0.0",
              {"sources": {}, "immediate_retries": 0, "source_retries": 0, "kind": "inlet"},
              [{"func": "f", "name": "r", "parents": []}])
    driver = Driver(db, tmp_path, "http://x", NoopLauncher())
    driver.set_duck("p@1", flock_mode="always", flock_engine=ENGINE)

    driver.tap("p@1")
    job = next(j for j in driver.jobs["p@1"] if j["kind"] == "begin_run")
    assert job["flock"]["DUCKSTRING_FLOCK_MODE"] == "always"
    assert job["flock"]["DUCKSTRING_FLOCK_ENGINE"] == ENGINE
    assert job["flock"]["REC_TOKEN"] == "tok-1"


# ─── using them (Duck side) ──────────────────────────────────────────────────────


def _run(tmp_path, job_flock, f):
    """One run of a Ripple with a builder terminal, as a Duck with no Flock environment runs it."""
    from duckstring.duck.executor import RippleExecutor

    ex = RippleExecutor("p", 1, "1.0.0", "ponds/p/1.0.0", tmp_path)
    try:
        ex.begin_run_inputs(f, {}, flock=job_flock)
        settings = ex.flock_for(f)
    finally:
        ex.shutdown()
    import os

    env = {**os.environ, **settings}
    con = duckdb.connect(str(tmp_path / f"reg-{f.hour}.duckdb"))
    pond = Pond(name="p", version="1", con=con, root=tmp_path, f=f,
                flock=env.get("DUCKSTRING_FLOCK_MODE"), flock_env=env)
    pond.merge_table("src", con.sql("SELECT range AS id, range % 7 AS v FROM range(100)"), pk="id")
    pond.trickle("src").select("s0.id, s0.v").merge("out", pk="id")
    return con.execute("SELECT count(*) FROM out").fetchone()[0]


def test_a_duck_with_no_flock_environment_dispatches_on_the_jobs_settings(tmp_path, monkeypatch):
    _clear_flock_env(monkeypatch)
    (tmp_path / "ponds" / "p" / "1.0.0").mkdir(parents=True)
    (tmp_path / "ponds" / "p" / "1.0.0" / "pond.toml").write_text('[pond]\nname = "p"\nversion = "1.0.0"\n')
    RecordingEngine.seen.clear()

    job = flock.job_settings({"flock_mode": "always", "flock_engine": ENGINE},
                             environ={}, secret=lambda n: "tok-1")
    assert _run(tmp_path, job, datetime(2026, 7, 19, 1, tzinfo=UTC)) == 100
    # The secret is rotated between runs: the next run's job carries, and the engine uses, the new one.
    job = flock.job_settings({"flock_mode": "always", "flock_engine": ENGINE},
                             environ={}, secret=lambda n: "tok-2")
    assert _run(tmp_path, job, datetime(2026, 7, 19, 2, tzinfo=UTC)) == 100
    assert RecordingEngine.seen == ["tok-1", "tok-2"]
