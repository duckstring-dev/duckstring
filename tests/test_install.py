"""`duckstring install` (plans/records-and-media.md): plans per machine, finding a server, extensions, extras."""

from __future__ import annotations

import gzip
import io
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from duckstring import install as inst
from duckstring.cli import app


def _fake_server(bin_dir: Path, major: int) -> Path:
    bin_dir.mkdir(parents=True)
    (bin_dir / "pg_ctl").write_text(f"#!/bin/sh\necho 'pg_ctl (PostgreSQL) {major}.2'\n")
    (bin_dir / "pg_ctl").chmod(0o755)
    (bin_dir / "postgres").write_text("#!/bin/sh\n")
    (bin_dir / "postgres").chmod(0o755)
    return bin_dir


@pytest.fixture
def no_servers(monkeypatch):
    """No PostgreSQL anywhere but where a test puts one."""
    monkeypatch.delenv("DUCKSTRING_PG_BIN", raising=False)
    monkeypatch.setattr(inst.shutil, "which", lambda name: None)
    real = inst._pg_candidates
    monkeypatch.setattr(inst, "_pg_candidates",
                        lambda major: [p for p in real(major) if not str(p).startswith(("/opt", "/usr", "/Applications"))])


def test_a_server_is_found_by_its_major(tmp_path, monkeypatch, no_servers):
    bin_dir = _fake_server(tmp_path / "pg17", 17)
    monkeypatch.setenv("DUCKSTRING_PG_BIN", str(bin_dir))
    assert inst.find_postgres(17) == (bin_dir, 17)
    assert inst.find_postgres(18) is None
    assert inst.find_postgres() == (bin_dir, 17)


def test_a_present_server_is_recorded_for_the_catchment(tmp_path, monkeypatch, no_servers):
    bin_dir = _fake_server(tmp_path / "pg18", 18)
    monkeypatch.setenv("DUCKSTRING_PG_BIN", str(bin_dir))
    result = CliRunner().invoke(app, ["install", "postgres"])
    assert result.exit_code == 0, result.output
    assert "installed" in result.output
    from duckstring.cli.config import load_config
    assert load_config()["install"]["postgres"] == str(bin_dir)
    monkeypatch.delenv("DUCKSTRING_PG_BIN")
    assert inst.find_postgres(18) == (bin_dir, 18)  # through the record alone


def _linux(monkeypatch, release: dict[str, str]):
    monkeypatch.setattr(inst.platform, "system", lambda: "Linux")
    monkeypatch.setattr(inst.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(inst, "_os_release", lambda: release)


def test_debian_installs_from_pgdg_without_its_default_cluster(monkeypatch, no_servers):
    _linux(monkeypatch, {"ID": "ubuntu", "ID_LIKE": "debian"})
    plan = inst.postgres_plan(18)
    assert plan.state == "missing"
    commands = [" ".join(s.argv) for s in plan.steps]
    assert any("apt.postgresql.org.sh" in c for c in commands)
    cluster = next(i for i, c in enumerate(commands) if "create_main_cluster = false" in c)
    server = next(i for i, c in enumerate(commands) if c.endswith("postgresql-18"))
    assert cluster < server  # the default cluster is turned off before the server is installed
    assert all(s.root for s in plan.steps)


def test_the_rhel_family_installs_from_the_pgdg_yum_repository(monkeypatch, no_servers):
    _linux(monkeypatch, {"ID": "rocky", "ID_LIKE": "rhel centos fedora", "VERSION_ID": "9.4"})
    plan = inst.postgres_plan(17)
    commands = [" ".join(s.argv) for s in plan.steps]
    assert "EL-9-x86_64/pgdg-redhat-repo-latest.noarch.rpm" in commands[0]
    assert commands[-1] == "dnf install -y postgresql17-server"


def test_an_unknown_system_is_told_where_to_get_postgres(monkeypatch, no_servers):
    _linux(monkeypatch, {"ID": "amzn", "VERSION_ID": "2023"})
    plan = inst.postgres_plan(18)
    assert plan.state == "unsupported" and inst.POSTGRES_DOWNLOAD in plan.detail
    monkeypatch.setattr(inst.platform, "system", lambda: "Windows")
    assert inst.postgres_plan(18).state == "unsupported"


def test_macos_uses_homebrew(monkeypatch, no_servers):
    monkeypatch.setattr(inst.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(inst.shutil, "which", lambda name: "/opt/homebrew/bin/brew" if name == "brew" else None)
    plan = inst.postgres_plan(18)
    assert [s.argv for s in plan.steps] == [["/opt/homebrew/bin/brew", "install", "postgresql@18"]]
    assert not any(s.root for s in plan.steps)


def test_steps_needing_root_are_refused_without_it(monkeypatch, no_servers):
    _linux(monkeypatch, {"ID": "debian"})
    monkeypatch.setattr(inst.os, "geteuid", lambda: 1000)
    ran = []
    monkeypatch.setattr(inst.subprocess, "run", lambda *a, **k: ran.append(a))
    result = CliRunner().invoke(app, ["install", "postgres", "--yes"])
    assert result.exit_code == 1
    assert "need root" in result.output
    assert ran == []


def test_a_dry_run_prints_the_steps_and_runs_nothing(monkeypatch, no_servers):
    _linux(monkeypatch, {"ID": "debian"})
    ran = []
    monkeypatch.setattr(inst.subprocess, "run", lambda *a, **k: ran.append(a))
    result = CliRunner().invoke(app, ["install", "postgres", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "$ apt-get install -y --no-install-recommends postgresql-18" in result.output
    assert "'create_main_cluster = false'" in result.output  # quoted as a shell would need it
    assert ran == []


def test_extensions_are_fetched_under_their_canonical_name(tmp_path, monkeypatch):
    monkeypatch.setattr(inst.Path, "home", lambda: tmp_path)
    fetched = []

    def fake_urlopen(request, timeout):
        fetched.append(request.full_url)
        return io.BytesIO(gzip.compress(b"extension bytes"))

    monkeypatch.setattr(inst.urllib.request, "urlopen", fake_urlopen)
    plans = inst.extension_plans(["postgres", "httpfs"])
    assert [p.state for p in plans] == ["missing", "missing"]
    inst.run(plans, lambda line: None)
    directory = inst.extension_dir()
    assert (directory / "postgres_scanner.duckdb_extension").read_bytes() == b"extension bytes"
    assert fetched[0].endswith("/postgres_scanner.duckdb_extension.gz")
    assert [p.state for p in inst.extension_plans(["postgres", "httpfs"])] == ["installed", "installed"]


def test_an_extension_name_is_checked():
    with pytest.raises(inst.InstallError):
        inst.extension_plans(["../evil"])


def test_an_extra_installs_into_this_environment_pinned_to_this_duckstring(monkeypatch):
    monkeypatch.setattr(inst, "extra_present", lambda e: e == "lineage")
    plan = inst.extra_plan(["dbt"])
    argv = plan.steps[0].argv
    assert argv[1:5] == ["pip", "install", "--python", sys.executable]
    spec = " ".join(argv[5:])
    assert "dbt" in spec and "lineage" in spec  # keeps the extras already present
    monkeypatch.setattr(inst, "extra_present", lambda e: True)
    assert inst.extra_plan(["dbt"]).state == "installed"
    with pytest.raises(inst.InstallError):
        inst.extra_plan(["nonsense"])


def test_with_no_component_every_state_is_listed():
    result = CliRunner().invoke(app, ["install"])
    assert result.exit_code == 0, result.output
    for name in ("postgres", "extension httpfs", "dbt", "aws"):
        assert name in result.output
