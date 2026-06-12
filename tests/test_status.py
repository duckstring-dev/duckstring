from __future__ import annotations

import io
import zipfile

import httpx

from duckstring.cli import app
from duckstring.cli.status import _ancestors, _filter_for_pond


def _deploy(url: str, *, name: str, version: str, kind: str, sources: dict[str, str] | None = None):
    """Deploy a pond. sources maps {pond_name: min_version_string} e.g. {"inlet": "1.0.0"}."""
    toml = f'[pond]\nname = "{name}"\nversion = "{version}"\n'
    if kind != "pond":
        toml += f'type = "{kind}"\n'
    if sources:
        toml += "\n[sources]\n"
        for src_name, min_ver in sources.items():
            toml += f'{src_name} = "{min_ver}"\n'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pond.toml", toml)
    httpx.post(
        f"{url}/api/deploy",
        files={"pond": ("pond.zip", buf.getvalue(), "application/zip")},
        data={"name": name, "version": version, "type": kind},
    )


def test_status_empty_ponds_message(runner, live_catchment):
    result = runner.invoke(app, ["status", "--once"])
    assert result.exit_code == 0
    assert "No Ponds" in result.output


def test_status_explicit_catchment(runner, live_catchment):
    result = runner.invoke(app, ["status", "--once", "-c", "dev"])
    assert result.exit_code == 0


def test_status_renders_pond_table(runner, live_catchment):
    _deploy(live_catchment, name="outlet", version="1.0.0", kind="outlet")
    result = runner.invoke(app, ["status", "--once"])
    assert result.exit_code == 0
    assert "outlet" in result.output
    assert "1.0.0" in result.output


def test_status_default_shows_active_only(runner, live_catchment):
    _deploy(live_catchment, name="inlet", version="1.0.0", kind="inlet")
    _deploy(live_catchment, name="inlet", version="1.1.0", kind="inlet")
    result = runner.invoke(app, ["status", "--once"])
    assert result.exit_code == 0
    assert "1.1.0" in result.output
    assert "1.0.0" not in result.output


def test_status_shows_both_majors(runner, live_catchment):
    # Two major lines of one name are independent live Ponds — both appear in status.
    _deploy(live_catchment, name="inlet", version="1.0.0", kind="inlet")
    _deploy(live_catchment, name="inlet", version="2.0.0", kind="inlet")
    result = runner.invoke(app, ["status", "--once"])
    assert result.exit_code == 0
    assert "1.0.0" in result.output
    assert "2.0.0" in result.output


def test_status_unknown_catchment_exits(runner):
    result = runner.invoke(app, ["status", "-c", "nonexistent"])
    assert result.exit_code != 0


def test_status_no_default_exits(runner):
    result = runner.invoke(app, ["status"])
    assert result.exit_code != 0


# --- Unit tests for _ancestors ---

def test_ancestors_root_only():
    assert _ancestors({"a"}, []) == {"a"}


def test_ancestors_chain():
    edges = [("a", "b"), ("b", "c")]
    assert _ancestors({"c"}, edges) == {"a", "b", "c"}


def test_ancestors_diamond():
    edges = [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")]
    assert _ancestors({"d"}, edges) == {"a", "b", "c", "d"}


def test_ancestors_excludes_downstream():
    edges = [("a", "b"), ("b", "c")]
    assert _ancestors({"b"}, edges) == {"a", "b"}


# --- Unit tests for _filter_for_pond ---

def _make_ponds(*specs):
    return [{"id": f"{n}@{v.split('.')[0]}", "name": n, "major": int(v.split(".")[0]), "version": v,
             "kind": "pond", "status": "idle", "gen": 0}
            for n, v in specs]


def test_filter_for_pond_name_only():
    ponds = _make_ponds(("inlet", "1.0.0"), ("outlet", "1.0.0"), ("other", "1.0.0"))
    edges = [("inlet@1", "outlet@1")]
    result_ponds, result_edges = _filter_for_pond(ponds, edges, "outlet", None, None)
    names = {p["name"] for p in result_ponds}
    assert names == {"inlet", "outlet"}
    assert ("other", "1.0.0") not in [(p["name"], p["version"]) for p in result_ponds]
    assert result_edges == [("inlet@1", "outlet@1")]


def test_filter_for_pond_excludes_unrelated():
    ponds = _make_ponds(("a", "1.0.0"), ("b", "1.0.0"), ("c", "1.0.0"))
    edges = [("a@1", "b@1")]
    result_ponds, _ = _filter_for_pond(ponds, edges, "b", None, None)
    assert {p["name"] for p in result_ponds} == {"a", "b"}


def test_filter_for_pond_major():
    ponds = _make_ponds(("outlet", "1.0.0"), ("outlet", "2.0.0"))
    edges: list = []
    result_ponds, _ = _filter_for_pond(ponds, edges, "outlet", 1, None)
    assert len(result_ponds) == 1
    assert result_ponds[0]["version"] == "1.0.0"


def test_filter_for_pond_version_str():
    ponds = _make_ponds(("outlet", "1.0.0"), ("outlet", "2.0.0"))
    edges: list = []
    result_ponds, _ = _filter_for_pond(ponds, edges, "outlet", None, "2.0.0")
    assert len(result_ponds) == 1
    assert result_ponds[0]["version"] == "2.0.0"


def test_filter_for_pond_unknown_returns_empty():
    ponds = _make_ponds(("inlet", "1.0.0"))
    edges: list = []
    result_ponds, result_edges = _filter_for_pond(ponds, edges, "ghost", None, None)
    assert result_ponds == []
    assert result_edges == []


# --- Integration tests ---

def test_status_pond_arg_single(runner, live_catchment):
    _deploy(live_catchment, name="myoutlet", version="1.0.0", kind="outlet")
    result = runner.invoke(app, ["status", "--once", "myoutlet"])
    assert result.exit_code == 0
    assert "myoutlet" in result.output


def test_status_pond_arg_unknown_exits(runner, live_catchment):
    result = runner.invoke(app, ["status", "--once", "nonexistent"])
    assert result.exit_code != 0
    assert "not found" in result.output


def test_status_pond_arg_filters_upstream(runner, live_catchment):
    _deploy(live_catchment, name="src", version="1.0.0", kind="inlet")
    _deploy(live_catchment, name="mid", version="1.0.0", kind="pond", sources={"src": "1.0.0"})
    _deploy(live_catchment, name="out", version="1.0.0", kind="outlet", sources={"mid": "1.0.0"})
    _deploy(live_catchment, name="unrelated", version="1.0.0", kind="inlet")

    result = runner.invoke(app, ["status", "--once", "out"])
    assert result.exit_code == 0
    assert "out" in result.output
    assert "mid" in result.output
    assert "src" in result.output
    assert "unrelated" not in result.output


def test_status_pond_arg_inlet_filter(runner, live_catchment):
    _deploy(live_catchment, name="src", version="1.0.0", kind="inlet")
    _deploy(live_catchment, name="out", version="1.0.0", kind="outlet", sources={"src": "1.0.0"})

    result = runner.invoke(app, ["status", "--once", "src"])
    assert result.exit_code == 0
    assert "src" in result.output
    assert "out" not in result.output


def test_status_default_uses_live_mode(runner, live_catchment, monkeypatch):
    # Default invocation should call _run_live (not the --once path), staying open until Ctrl+C.
    import importlib
    status_mod = importlib.import_module("duckstring.cli.status")

    calls = []
    monkeypatch.setattr(status_mod, "_run_live", lambda *a, **kw: calls.append(kw))

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert calls and calls[0]["watch"] is True
