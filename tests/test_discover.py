"""Deploy-time discovery in a subprocess (plans/pond-environments.md): the Catchment never imports Pond
code itself, and a Pond whose code can't be loaded fails its deploy instead of registering no Ripples."""

from __future__ import annotations

import io
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest

from duckstring.discover import DiscoveryError, discover

pytestmark = pytest.mark.timeout(20)

_TOML = '[pond]\nname = "p"\nversion = "1.0.0"\ntype = "inlet"\n'

_GOOD = """
    from duckstring import ripple

    print("output from the Pond's import")  # must not corrupt the result

    @ripple
    def a(pond):
        pond.write_table("a", pond.con.sql("SELECT 1 AS x"))

    @ripple(parents=[a], always_run=True)
    def b(pond):
        pond.write_table("b", pond.con.sql("SELECT x FROM a"))
"""


def _pond(tmp_path: Path, pond_py: str, toml: str = _TOML) -> Path:
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "pond.toml").write_text(toml)
    (tmp_path / "src" / "pond.py").write_text(textwrap.dedent(pond_py))
    return tmp_path


def test_discovers_ripples_by_name(tmp_path):
    found = discover(sys.executable, _pond(tmp_path, _GOOD))
    assert found["ripples"] == [
        {"name": "a", "parents": [], "always_run": False},
        {"name": "b", "parents": ["a"], "always_run": True},
    ]


def test_import_error_is_reported_with_its_traceback(tmp_path):
    with pytest.raises(DiscoveryError) as err:
        discover(sys.executable, _pond(tmp_path, "import not_a_real_package_xyz\n"))
    assert "ModuleNotFoundError" in str(err.value)
    assert "not_a_real_package_xyz" in err.value.detail


def test_sys_exit_in_pond_code_is_a_failure(tmp_path):
    with pytest.raises(DiscoveryError, match="SystemExit"):
        discover(sys.executable, _pond(tmp_path, "raise SystemExit(3)\n"))


def test_a_hanging_import_times_out(tmp_path):
    with pytest.raises(DiscoveryError, match="longer than"):
        discover(sys.executable, _pond(tmp_path, "import time\ntime.sleep(30)\n"), timeout=1)


def test_an_interpreter_without_duckstring_is_reported(tmp_path):
    fake = tmp_path / "python"
    fake.write_text("#!/bin/sh\necho 'No module named duckstring' >&2\nexit 1\n")
    fake.chmod(0o755)
    with pytest.raises(DiscoveryError) as err:
        discover(fake, _pond(tmp_path / "p", _GOOD))
    assert "No module named duckstring" in err.value.detail


def test_lineage_uses_the_source_catalog(tmp_path):
    pond_py = """
        from duckstring import ripple

        @ripple
        def priced(pond):
            (pond.trickle("orders.order_line", p=1.0).alias("ol")
             .join(pond.trickle("prices.price", p=1.0).alias("pr"), on={"pid": "id"})
             .select("ol.id AS id, pr.unit AS unit")
             .merge("priced", pk="id"))
    """
    found = discover(sys.executable, _pond(tmp_path, pond_py), {"orders.order_line": ["id", "pid"]})
    assert ["priced", "id", "exact", "orders.order_line", "id"] in found["lineage"]
    assert ["priced", "unit", "exact", "prices.price", "unit"] in found["lineage"]


# ── through the deploy route ─────────────────────────────────────────────────


def _zip(pond_py: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pond.toml", _TOML)
        zf.writestr("src/pond.py", textwrap.dedent(pond_py))
    return buf.getvalue()


def _deploy(client, pond_py: str):
    return client.post(
        "/api/deploy",
        files={"pond": ("pond.zip", _zip(pond_py), "application/zip")},
        data={"name": "p", "version": "1.0.0", "type": "inlet"},
    )


def test_deploy_fails_when_the_code_cant_be_imported(catchment_client):
    r = _deploy(catchment_client, "import not_a_real_package_xyz\n")
    assert r.status_code == 422
    assert "not_a_real_package_xyz" in r.json()["detail"]
    root = catchment_client.app.state.root
    assert not (root / "ponds" / "p" / "1.0.0").exists()
    assert not [p for p in (root / "ponds").iterdir() if p.name.startswith(".staging")]


def test_a_failed_redeploy_leaves_the_deployed_copy(catchment_client):
    assert _deploy(catchment_client, _GOOD).status_code == 200
    pond_py = catchment_client.app.state.root / "ponds" / "p" / "1.0.0" / "src" / "pond.py"
    before = pond_py.read_text()

    assert _deploy(catchment_client, "raise RuntimeError('broken')\n").status_code == 422
    assert pond_py.read_text() == before
    names = {r[0] for r in catchment_client.app.state.db.execute("SELECT name FROM ripple")}
    assert names == {"a", "b"}
