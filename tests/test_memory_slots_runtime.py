"""Memory slots end to end on real Ducks (plans/memory-budget.md): a Duck runs its Ripples under the DuckDB
memory limit its slots are worth, and Ponds beyond the slots wait their turn."""

from __future__ import annotations

import textwrap
import time

import duckdb
import httpx
import pytest
from test_runtime import _free_port, _serve, _wait, _zip_dir

from duckstring.dataplane import ParquetDataPlane

pytestmark = pytest.mark.timeout(120)

_CODE = '''
import time
from duckstring import ripple

@ripple
def r(pond):
    limit = pond.con.execute("SELECT current_setting('memory_limit')").fetchall()[0][0]
    time.sleep(1.5)
    pond.con.execute(f"CREATE OR REPLACE TABLE seen AS SELECT '{limit}' AS memory_limit, now() AS at")
'''


@pytest.fixture
def slotted(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("slots_root")
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    monkeypatch.delenv("DUCKSTRING_DISABLE_DUCKS", raising=False)
    monkeypatch.setenv("DUCKSTRING_CATCHMENT_URL", url)
    monkeypatch.setenv("DUCKSTRING_RUN_SLOTS", "2")
    monkeypatch.setenv("DUCKSTRING_MEMORY_BUDGET", "3GB")
    server, thread = _serve(root, port)
    yield url, root
    server.should_exit = True
    thread.join(timeout=5)


def test_ducks_run_under_their_slots_limit_and_wait_their_turn(slotted, tmp_path):
    url, root = slotted
    names = ("m1", "m2", "m3")
    for name in names:
        d = tmp_path / name
        (d / "src").mkdir(parents=True)
        (d / "pond.toml").write_text(f'[pond]\nname = "{name}"\nversion = "1.0.0"\ntype = "inlet"\n')
        (d / "src" / "pond.py").write_text(textwrap.dedent(_CODE))
        r = httpx.post(f"{url}/api/deploy", files={"pond": ("pond.zip", _zip_dir(d), "application/zip")},
                       data={"name": name, "version": "1.0.0", "type": "inlet"}, timeout=15.0)
        assert r.status_code == 200, r.text
    for name in names:
        httpx.post(f"{url}/api/ponds/{name}/refresh", timeout=5.0).raise_for_status()
    time.sleep(0.5)
    status = {p["name"]: p for p in httpx.get(f"{url}/api/status", timeout=5.0).json()["ponds"]}
    assert [status[n]["status"] for n in names].count("waiting") == 1  # two slots, three Ponds
    assert status["m3"]["memory"] == {"slots": 0, "of": 2, "need": 1, "waiting_since": status["m3"]["memory"]
                                      ["waiting_since"], "position": 1, "declared_bytes": None}

    def published(name):
        try:
            sql = ParquetDataPlane().read_select(root / "ponds" / name / "m1" / "data", "seen")
        except FileNotFoundError:
            return None
        return duckdb.sql(sql).fetchall()[0]

    _wait(lambda: all(published(n) for n in names), timeout=60)
    seen = {n: published(n) for n in names}
    for name in names:
        assert seen[name][0] in ("1.3 GiB", "1.4 GiB"), seen  # one 1.5 GB slot each, not DuckDB's 80% of RAM
    first, *_ = sorted(seen, key=lambda n: seen[n][1])
    assert seen["m3"][1] > min(seen["m1"][1], seen["m2"][1])  # the third ran after a slot freed
    assert first in ("m1", "m2")
