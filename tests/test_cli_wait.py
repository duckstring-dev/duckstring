"""Commands for scripts and agents (plans/showcase-fixes.md item 12): without a terminal, ``status`` prints one
snapshot and the one-shot commands wait for their outcome, and ``--wait`` exits 0, 1 (naming what failed) or 2
(timed out). Every showcase session wrote polling scripts against /api/runs, and a ``status`` in a script hung
for its full timeout. The Catchment here is a scripted fake: Ducks don't run in the test suite."""

from __future__ import annotations

import json
import sys

import pytest

from duckstring.cli import app

T0 = "2026-11-15T05:00:00+00:00"
T1 = "2026-11-15T05:00:10+00:00"


class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status
        self.content = json.dumps(body).encode()

    def json(self):
        return self._body


class FakeCatchment:
    """``/api/status`` and ``/api/runs`` replayed from a script: each poll of the status takes the next step."""

    def __init__(self, steps: list[dict], runs_before: list[dict], runs_after: list[dict]):
        self.steps, self.runs_before, self.runs_after = steps, runs_before, runs_after
        self.polls = 0
        self.posted: list[str] = []

    def get(self, url, auth=None, params=None, allow=(), **kw):
        if url.endswith("/api/status"):
            step = self.steps[min(self.polls, len(self.steps) - 1)]
            self.polls += 1
            return _Resp({"ponds": [step], "edges": []})
        if url.endswith("/api/runs"):
            return _Resp({"runs": self.runs_after if self.posted else self.runs_before})
        if url.endswith("/reports"):
            self.reports_asked = getattr(self, "reports_asked", 0) + 1
            return _Resp({"reports": [{"f": T1, "reports": {"newest_hour": "05:00", "rows": 12}}]})
        raise AssertionError(url)

    def post(self, url, auth=None, **kw):
        self.posted.append(url)
        return _Resp({"ok": True, "requested": T1})


def _entry(status, end_f=None, **kw):
    return {"id": "reports@1", "name": "reports", "major": 1, "version": "1.0.0", "status": status,
            "end_f": end_f, "runs_completed": kw.pop("runs_completed", 0), **kw}


def _run(pond, f, status, ripples=()):
    return {"id": pond, "pond": pond.split("@")[0], "version": "1.0.0", "f": f, "status": status,
            "started_at": f, "finished_at": f, "error": None,
            "ripples": [{"ripple": r, "status": s, "error": e, "retry": 0, "started_at": f, "finished_at": f}
                        for r, s, e in ripples]}


@pytest.fixture
def fake(monkeypatch, dev_catchment):
    def install(*args):
        cat = FakeCatchment(*args)
        monkeypatch.setattr("duckstring.cli._http.get", cat.get)
        monkeypatch.setattr("duckstring.cli._http.post", cat.post)
        monkeypatch.setattr("duckstring.cli.wait._POLL_S", 0.0)
        return cat
    return install


def test_refresh_now_without_a_terminal_waits_and_reports_success(runner, fake):
    cat = fake([_entry("running"), _entry("running"), _entry("idle", end_f=T1)],
               [_run("reports@1", T0, "success")],
               [_run("reports@1", T0, "success"), _run("sales@1", T1, "success"), _run("reports@1", T1, "success")])
    result = runner.invoke(app, ["refresh", "now", "reports"])
    assert result.exit_code == 0, result.output
    assert "fresh as of 2026-11-15T05:00:10+00:00, after 2 Runs" in result.output
    assert cat.polls >= 3  # it waited for the target to reach the requested freshness


def test_a_failed_run_upstream_exits_1_and_names_the_ripple(runner, fake):
    fake([_entry("blocked", blocked_by=["sales@1"])], [],
         [_run("sales@1", T1, "failed", [("join_lines", "failed", "Binder Error: column x not found")])])
    result = runner.invoke(app, ["refresh", "now", "reports", "--wait"])
    assert result.exit_code == 1
    assert "sales@1" in result.output and "join_lines" in result.output and "column x not found" in result.output


def test_a_blocked_target_with_no_new_failure_says_why(runner, fake):
    fake([_entry("blocked", blocked_by=["sales@1"])], [], [])
    result = runner.invoke(app, ["control", "wake", "reports", "--wait"])
    assert result.exit_code == 1 and "blocked by sales@1" in result.output


def test_wait_times_out_with_exit_2(runner, fake):
    fake([_entry("running")], [], [])
    result = runner.invoke(app, ["control", "force", "reports", "--wait", "--timeout", "0s"])
    assert result.exit_code == 2 and "Timed out" in result.output


def test_rebuild_waits_for_the_run_that_rebuilds(runner, fake):
    fake([_entry("idle", rebuild_pending=True), _entry("running", rebuild_pending=True),
          _entry("idle", end_f=T1, rebuild_pending=False, runs_completed=1)], [], [_run("reports@1", T1, "success")])
    result = runner.invoke(app, ["control", "rebuild", "reports", "--wait"])
    assert result.exit_code == 0, result.output
    assert "Rebuild" in result.output


def test_live_keeps_the_status_view_without_a_terminal(runner, fake, monkeypatch):
    fake([_entry("idle")], [], [])
    opened = []
    monkeypatch.setattr(sys.modules["duckstring.cli.status"], "_run_live", lambda *a, **k: opened.append(k))
    assert runner.invoke(app, ["refresh", "now", "reports", "--view"]).exit_code == 0
    assert runner.invoke(app, ["status", "--follow"]).exit_code == 0
    assert len(opened) == 2


def test_status_without_a_terminal_prints_once_and_json_has_every_field(runner, fake, monkeypatch):
    fake([_entry("idle", end_f=T1, rebuild_pending=False)], [], [])
    monkeypatch.setattr(sys.modules["duckstring.cli.status"], "_run_live",
                        lambda *a, **k: pytest.fail("opened the live view"))
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0 and "reports" in result.output
    data = json.loads(runner.invoke(app, ["status", "--json"]).output)
    assert data["ponds"][0]["rebuild_pending"] is False and data["ponds"][0]["end_f"] == T1


def test_runs_lists_runs_and_ripples(runner, fake):
    fake([_entry("idle")], [_run("reports@1", T1, "failed", [("score", "failed", "boom"), ("load", "success", None)])],
         [])
    result = runner.invoke(app, ["runs", "reports", "--ripples"])
    assert result.exit_code == 0, result.output
    assert "reports@1" in result.output and "score" in result.output and "boom" in result.output
    data = json.loads(runner.invoke(app, ["runs", "reports", "--json"]).output)
    assert data[0]["status"] == "failed"


def test_deploy_waits_for_the_new_version_to_complete_a_run(runner, fake, tmp_path, monkeypatch):
    (tmp_path / "pond.toml").write_text('[pond]\nname = "reports"\nversion = "1.1.0"\n')
    monkeypatch.chdir(tmp_path)
    new = _run("reports@1", T1, "success")
    new["version"] = "1.1.0"
    cat = fake([_entry("idle", version="1.1.0")], [_run("reports@1", T0, "success")],
               [new, _run("reports@1", T0, "success")])
    result = runner.invoke(app, ["pond", "deploy", "--yes", "--wait"])
    assert result.exit_code == 0, result.output
    assert any(u.endswith("/api/deploy") for u in cat.posted)
    assert "Version 1.1.0" in result.output


def test_runs_shows_a_superseded_run_as_neither_failed_nor_an_error(runner, fake):
    covered = _run("reports@1", T0, "superseded")
    covered["superseded_by"] = T1
    fake([_entry("idle")], [_run("reports@1", T1, "success"), covered], [])
    result = runner.invoke(app, ["runs", "reports"])
    assert result.exit_code == 0, result.output
    assert "superseded by the Run at 2026-11-15 05:00:10" in result.output and "failed" not in result.output


def test_runs_shows_what_each_run_reported(runner, fake):
    run = _run("reports@1", T1, "success")
    run["major"] = 1
    older = _run("reports@1", T0, "success")
    older["major"] = 1
    cat = fake([_entry("idle")], [run, older], [])
    result = runner.invoke(app, ["runs", "reports", "--reports"])
    assert result.exit_code == 0, result.output
    assert "newest_hour=05:00, rows=12" in result.output
    data = json.loads(runner.invoke(app, ["runs", "reports", "--reports", "--json"]).output)
    assert data[0]["reports"] == {"newest_hour": "05:00", "rows": 12} and data[1]["reports"] is None
    assert cat.reports_asked == 2  # one request per Pond line, per command
