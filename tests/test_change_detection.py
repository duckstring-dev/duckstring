"""Per-object change detection (plans/change-detection.md): the change ledger, ``pond.changed``,
``pond.detect_change`` and ``write_table``/``write_object(if_changed=True)``, the held export that keeps an
unchanged table's published version, and the Source-side ``changed`` for plain tables, Trickles and
Objects."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from duckstring.changes import ChangeLedger
from duckstring.core import MissingSourceAsset, Pond, RippleOrderError
from duckstring.dataplane import KEEP_ALL, ParquetDataPlane, fingerprint
from duckstring.engine.core import NEVER
from duckstring.objects import commit_objects
from duckstring.storage import LocalStorage
from duckstring.trickle.io import RUN_KEY, load_sidecar, part_name

F1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
F2 = F1 + timedelta(hours=1)
F3 = F1 + timedelta(hours=2)


class Producer:
    """A Pond's own runs against a persistent registry: handles per Ripple, then the Duck's publish (the
    data plane export and the Object commit, both honouring the Run's held set)."""

    def __init__(self, root, name="src"):
        self.root = root
        self.name = name
        self.data_dir = LocalStorage(root / "ponds" / name / "m1" / "data")
        self.staging = root / "staging" / name
        self.con = duckdb.connect(str(root / f"{name}.duckdb"))
        self.ledger = ChangeLedger()

    def handle(self, f, ripple="r", **kw):
        return Pond(self.name, "1.0.0", self.con, self.root, f=f, own_data_dir=self.data_dir,
                    staging_dir=self.staging, change_sink=self.ledger.begin(f, ripple), ripple=ripple, **kw)

    def publish(self, f, retain_from=KEEP_ALL):
        held = self.ledger.held(f)
        ParquetDataPlane().export(self.con, self.data_dir, f=f, retain_from=retain_from, held=held)
        commit_objects(self.staging, self.data_dir, f, held)
        self.ledger.drop(f)

    def sidecar(self):
        return load_sidecar(self.data_dir)

    def versions(self, table):
        return sorted(p.name for p in (self.data_dir.root / f"{table}__v").glob("*.parquet"))


def _sink(root, f, previous_f, pin=None, source="src"):
    return Pond("snk", "1.0.0", duckdb.connect(), root, source_majors={source: 1}, f=f, previous_f=previous_f,
                source_f={source: (pin or f).isoformat()})


# ─── the ledger ──────────────────────────────────────────────────────────────────


def test_a_retry_clears_the_ripples_earlier_attempt():
    ledger = ChangeLedger()
    ledger.begin(F1, "a").record_write("t", True)
    assert ledger.changed(F1, "t")
    ledger.begin(F1, "a")  # the immediate retry relaunches the Ripple
    assert not ledger.changed(F1, "t") and not ledger.any_changed(F1)


def test_concurrent_runs_do_not_mix():
    ledger = ChangeLedger()
    ledger.begin(F1, "a").record_write("t", True)
    ledger.begin(F2, "a").record_check("t", True)
    assert ledger.changed(F1, "t") and not ledger.changed(F2, "t")
    assert ledger.held(F1) == frozenset() and ledger.held(F2) == {"t"}
    ledger.drop(F1)
    assert not ledger.any_changed(F1) and ledger.held(F2) == {"t"}


def test_held_needs_every_check_unchanged_and_no_write():
    ledger = ChangeLedger()
    a, b = ledger.begin(F1, "a"), ledger.begin(F1, "b")
    a.record_check("x", True)
    a.record_check("y", True)
    b.record_write("y", True)          # written elsewhere in the Run: published as usual
    a.record_check("z", True)
    b.record_check("z", False)         # one check saw a difference
    assert ledger.held(F1) == {"x"}


# ─── own objects ─────────────────────────────────────────────────────────────────


def test_writes_return_true_and_count_as_changed(tmp_path):
    p = Producer(tmp_path).handle(F1)
    assert p.changed() is False
    assert p.write_table("t", p.con.sql("SELECT 1 AS v")) is True
    assert p.write_object("model", b"weights") is True
    assert p.changed("t") and p.changed("model") and p.changed()
    assert not p.changed("other")  # untouched (or written with raw SQL): not seen


def test_a_trickle_write_counts_only_if_it_changed(tmp_path):
    prod = Producer(tmp_path)
    p = prod.handle(F1)
    p.merge_table("m", p.con.sql("SELECT 1 AS id, 'a' AS v"), pk="id")
    assert p.changed("m")
    prod.publish(F1)
    p2 = prod.handle(F2)
    p2.merge_table("m", p2.con.sql("SELECT 1 AS id, 'a' AS v"), pk="id")  # same state: no change
    p2.append_table("e", p2.con.sql("SELECT 1 AS id WHERE false"))       # nothing appended
    assert not p2.changed("m") and not p2.changed("e") and not p2.changed()


def test_a_builder_terminal_records_its_change(tmp_path):
    src = Producer(tmp_path, "src")
    s = src.handle(F1)
    s.merge_table("item", s.con.sql("SELECT 1 AS id, 'a' AS v"), pk="id")
    src.publish(F1)
    snk = Producer(tmp_path, "snk")

    def run(f, pf):
        p = snk.handle(f, source_majors={"src": 1}, previous_f=pf)
        p.trickle("src.item").merge("out", pk="id")
        out = p.changed("out")
        snk.publish(f)
        return out

    assert run(F1, NEVER)
    s2 = src.handle(F2)
    s2.merge_table("item", s2.con.sql("SELECT 1 AS id, 'a' AS v"), pk="id")
    src.publish(F2)
    assert not run(F2, F1)


def test_asking_about_an_own_table_before_its_writer_ran_raises(tmp_path):
    p = Pond("p", "1.0.0", duckdb.connect(), tmp_path, ripple="early", ancestors=set(),
             table_writers={"t": "late"})
    with pytest.raises(RippleOrderError):
        p.changed("t")


# ─── fingerprints ────────────────────────────────────────────────────────────────


def test_a_fingerprint_ignores_row_order_and_sees_duplicates_and_types():
    con = duckdb.connect()
    a = fingerprint(con.sql("SELECT * FROM (VALUES (1, 'x'), (2, 'y')) t(id, v)"))
    assert a == fingerprint(con.sql("SELECT * FROM (VALUES (2, 'y'), (1, 'x')) t(id, v)"))
    assert a != fingerprint(con.sql("SELECT * FROM (VALUES (1, 'x'), (2, 'y'), (2, 'y')) t(id, v)"))
    assert a != fingerprint(con.sql("SELECT id::BIGINT AS id, v FROM (VALUES (1, 'x'), (2, 'y')) t(id, v)"))
    assert a != fingerprint(con.sql("SELECT * FROM (VALUES (1, 'x'), (2, 'y')) t(id, w)"))
    empty = fingerprint(con.sql("SELECT 1 AS id WHERE false"))
    assert empty == fingerprint(con.sql("SELECT 2 AS id WHERE false"))
    assert empty != fingerprint(con.sql("SELECT 0 AS id"))


# ─── held overwrite tables ───────────────────────────────────────────────────────


def _snapshot(p, value):
    return p.con.sql(f"SELECT * FROM (VALUES ({value}, 'a'), (2, 'b')) t(id, v)")


def test_an_unchanged_table_is_held(tmp_path):
    prod = Producer(tmp_path)
    p = prod.handle(F1)
    assert p.write_table("t", _snapshot(p, 1), if_changed=True) is True  # nothing published yet
    prod.publish(F1)
    entry = prod.sidecar()["t"]
    assert entry["f"] == F1.isoformat() and entry["fp"].startswith("fp1-duckdb")

    p2 = prod.handle(F2)
    assert p2.detect_change("t", _snapshot(p2, 1)) is False
    assert p2.write_table("t", _snapshot(p2, 1), if_changed=True) is False
    assert not p2.changed("t")
    prod.publish(F2)
    assert prod.versions("t") == [part_name(F1)]  # no new version written
    sidecar = prod.sidecar()
    assert sidecar["t"]["f"] == F1.isoformat() and sidecar["t"]["fp"] == entry["fp"]
    assert sidecar[RUN_KEY] == {"f": F2.isoformat()}


def test_a_changed_table_publishes_a_new_version(tmp_path):
    prod = Producer(tmp_path)
    p = prod.handle(F1)
    p.write_table("t", _snapshot(p, 1), if_changed=True)
    prod.publish(F1)
    p2 = prod.handle(F2)
    assert p2.write_table("t", _snapshot(p2, 5), if_changed=True) is True
    assert p2.changed("t")
    prod.publish(F2)
    assert prod.versions("t") == [part_name(F1), part_name(F2)]
    assert prod.sidecar()["t"]["f"] == F2.isoformat()


def test_a_fingerprint_from_detect_change_carries_to_the_write(tmp_path):
    prod = Producer(tmp_path)
    p = prod.handle(F1)
    rel = _snapshot(p, 1)
    assert p.detect_change("t", rel)
    p.write_table("t", rel)  # the same relation object: its fingerprint is recorded with the write
    prod.publish(F1)
    assert "fp" in prod.sidecar()["t"]
    assert prod.handle(F2).detect_change("t", _snapshot(p, 1)) is False


def test_a_write_without_a_fingerprint_clears_it(tmp_path):
    prod = Producer(tmp_path)
    p = prod.handle(F1)
    p.write_table("t", _snapshot(p, 1), if_changed=True)
    prod.publish(F1)
    p2 = prod.handle(F2)
    p2.write_table("t", _snapshot(p2, 1))  # a plain write: no fingerprint
    prod.publish(F2)
    assert "fp" not in prod.sidecar()["t"]
    assert prod.handle(F3).detect_change("t", _snapshot(p2, 1)) is True


def test_a_failed_runs_write_left_in_the_registry_is_not_held(tmp_path):
    """Run 2 writes a new value and fails (nothing published). Run 3 computes the published value again:
    it matches the published fingerprint, but the Pond database still holds run 2's value, which later
    Ripples would read, so it must be written."""
    prod = Producer(tmp_path)
    p = prod.handle(F1)
    p.write_table("t", _snapshot(p, 1), if_changed=True)
    prod.publish(F1)
    p2 = prod.handle(F2)
    p2.write_table("t", _snapshot(p2, 9), if_changed=True)
    prod.ledger.drop(F2)  # the run failed: no publish
    p3 = prod.handle(F3)
    assert p3.detect_change("t", _snapshot(p3, 1)) is True
    assert p3.write_table("t", _snapshot(p3, 1), if_changed=True) is True
    assert p3.con.sql("SELECT id FROM t ORDER BY id").fetchall() == [(1,), (2,)]


def test_a_fingerprint_from_another_duckdb_version_is_changed(tmp_path, monkeypatch):
    prod = Producer(tmp_path)
    p = prod.handle(F1)
    p.write_table("t", _snapshot(p, 1), if_changed=True)
    prod.publish(F1)
    monkeypatch.setattr("duckstring.dataplane._fp_tag", lambda: "fp1-duckdb9.9")
    assert prod.handle(F2).detect_change("t", _snapshot(p, 1)) is True


def test_an_exact_check_compares_rows(tmp_path):
    prod = Producer(tmp_path)
    p = prod.handle(F1)
    p.write_table("t", _snapshot(p, 1), if_changed=True)
    prod.publish(F1)
    p2 = prod.handle(F2)
    assert p2.detect_change("t", p2.con.sql("SELECT * FROM (VALUES (2, 'b'), (1, 'a')) t(id, v)"), exact=True) is False
    assert p2.detect_change("t", _snapshot(p2, 3), exact=True) is True
    assert p2.detect_change("t", p2.con.sql("SELECT id::BIGINT AS id, v FROM t"), exact=True) is True


def test_nothing_published_means_changed(tmp_path):
    p = Producer(tmp_path).handle(F1)
    assert p.detect_change("t", _snapshot(p, 1)) is True
    assert p.detect_change("model", b"x") is True
    with pytest.raises(ValueError, match="own output"):
        p.detect_change("other.t", _snapshot(p, 1))


def test_retention_keeps_a_held_newest_version(tmp_path):
    prod = Producer(tmp_path)
    p = prod.handle(F1)
    p.write_table("t", _snapshot(p, 1), if_changed=True)
    prod.publish(F1)
    p2 = prod.handle(F2)
    p2.write_table("t", _snapshot(p2, 2), if_changed=True)
    prod.publish(F2)
    p3 = prod.handle(F3)
    assert p3.write_table("t", _snapshot(p3, 2), if_changed=True) is False
    prod.publish(F3, retain_from=None)  # keep only the newest: F2's, which F3 held
    assert prod.versions("t") == [part_name(F2)]
    assert duckdb.sql(ParquetDataPlane().read_select(prod.data_dir, "t")).fetchall() == [(2, "a"), (2, "b")]


def test_the_run_stamp_backs_the_plane_freshness(tmp_path):
    """A held table keeps an older ``f``, so the plane's freshness comes from the run stamp; otherwise a
    local publish would look stale against the Catchment's ``changed_f`` and be skipped for the data root."""
    from duckstring.catchment.driver import _plane_freshness
    from duckstring.catchment.registry import local_publish_f, resolve_data_dir

    prod = Producer(tmp_path)
    p = prod.handle(F1)
    p.write_table("t", _snapshot(p, 1), if_changed=True)
    prod.publish(F1)
    prod.handle(F2).write_table("t", _snapshot(p, 1), if_changed=True)
    prod.publish(F2)
    assert prod.sidecar()["t"]["f"] == F1.isoformat()
    assert local_publish_f(prod.data_dir) == F2.isoformat()
    remote = str(tmp_path / "remote")
    assert resolve_data_dir(tmp_path, "src", 1, remote, expected_f=F2.isoformat()).root == prod.data_dir.root
    # A sidecar written before the stamp: the newest entry.
    assert _plane_freshness({"a": {"f": F1.isoformat()}, "b": {"f": F2.isoformat()}}) == F2.isoformat()


def test_read_delta_is_empty_for_a_held_source(tmp_path):
    prod = Producer(tmp_path)
    p = prod.handle(F1)
    p.write_table("t", _snapshot(p, 1), if_changed=True)
    prod.publish(F1)
    prod.handle(F2).write_table("t", _snapshot(p, 1), if_changed=True)
    prod.publish(F2)
    delta = _sink(tmp_path, F2, F1).read_delta("src.t")
    assert not delta.is_full and delta.is_empty()
    assert _sink(tmp_path, F2, NEVER).read_delta("src.t").is_full


# ─── Objects ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["bytes", "file", "dir"])
def test_an_unchanged_object_is_held(tmp_path, kind):
    def payload(content: bytes):
        if kind == "bytes":
            return content
        d = tmp_path / f"payload-{content.decode()}"
        d.mkdir(exist_ok=True)
        if kind == "file":
            (d / "m.bin").write_bytes(content)
            return d / "m.bin"
        (d / "a.txt").write_bytes(content)
        (d / "sub").mkdir(exist_ok=True)
        (d / "sub" / "b.txt").write_bytes(b"fixed")
        return d

    prod = Producer(tmp_path)
    assert prod.handle(F1).write_object("model", payload(b"one"), if_changed=True) is True
    prod.publish(F1)
    entry = prod.sidecar()["objects"]["model"]
    assert entry["f"] == F1.isoformat() and len(entry["sha256"]) == 64

    p2 = prod.handle(F2)
    assert p2.detect_change("model", payload(b"one")) is False
    assert p2.write_object("model", payload(b"one"), if_changed=True) is False
    assert not p2.changed("model")
    prod.publish(F2)
    assert prod.sidecar()["objects"]["model"] == entry  # keeps the freshness of the run that changed it

    p3 = prod.handle(F3)
    assert p3.write_object("model", payload(b"two"), if_changed=True) is True
    prod.publish(F3)
    assert prod.sidecar()["objects"]["model"]["f"] == F3.isoformat()


def test_a_held_object_drops_a_payload_a_failed_run_left_staged(tmp_path):
    from duckstring.objects import read_object

    prod = Producer(tmp_path)
    prod.handle(F1).write_object("model", b"good")
    prod.publish(F1)
    prod.handle(F2).write_object("model", b"BAD")  # staged; the run fails, so it never commits
    prod.ledger.drop(F2)
    p3 = prod.handle(F3)
    assert p3.write_object("model", b"good", if_changed=True) is False
    prod.publish(F3)
    assert read_object(prod.data_dir, "model") == b"good"
    assert prod.sidecar()["objects"]["model"]["f"] == F1.isoformat()


def test_a_file_like_is_rewound_after_a_check(tmp_path):
    import io

    prod = Producer(tmp_path)
    prod.handle(F1).write_object("model", b"abc")
    prod.publish(F1)
    p2 = prod.handle(F2)
    buf = io.BytesIO(b"abd")
    assert p2.detect_change("model", buf) is True
    assert p2.write_object("model", buf) is True
    prod.publish(F2)
    from duckstring.objects import read_object

    assert read_object(prod.data_dir, "model") == b"abd"


def test_a_draw_does_not_reship_a_held_object(tmp_path):
    import io
    import zipfile

    from duckstring.catchment.routes.draw import _ship_objects

    prod = Producer(tmp_path)
    prod.handle(F1).write_object("model", b"abc")
    prod.publish(F1)
    prod.handle(F2).write_object("model", b"abc", if_changed=True)
    prod.publish(F2)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        _ship_objects(zf, prod.data_dir, prod.sidecar()["objects"], F1.isoformat())
    assert zipfile.ZipFile(buf).namelist() == []


# ─── Source-side changed ─────────────────────────────────────────────────────────


def test_a_source_table_changed_at_the_pin(tmp_path):
    prod = Producer(tmp_path)
    p = prod.handle(F1)
    p.write_table("t", _snapshot(p, 1), if_changed=True)
    p.write_table("u", _snapshot(p, 1), if_changed=True)
    prod.publish(F1)
    p2 = prod.handle(F2)
    p2.write_table("t", _snapshot(p2, 1), if_changed=True)  # held
    p2.write_table("u", _snapshot(p2, 7), if_changed=True)  # changed
    prod.publish(F2)

    sink = _sink(tmp_path, F2, F1)
    assert not sink.changed("src.t")
    assert sink.changed("src.u") and sink.changed("src.t", "src.u")
    assert not _sink(tmp_path, F2, F1, pin=F1).changed("src.u")  # pinned before u's new version
    assert _sink(tmp_path, F2, NEVER).changed("src.t")           # a first run: everything changed


def test_a_source_trickle_changed_in_the_window(tmp_path):
    prod = Producer(tmp_path)
    p = prod.handle(F1)
    p.append_table("e", p.con.sql("SELECT 1 AS id"))
    p.merge_table("m", p.con.sql("SELECT 1 AS id, 'a' AS v"), pk="id")
    prod.publish(F1)
    p2 = prod.handle(F2)
    p2.append_table("e", p2.con.sql("SELECT 2 AS id"))
    p2.merge_table("m", p2.con.sql("SELECT 1 AS id, 'a' AS v"), pk="id")  # no change
    prod.publish(F2)

    sink = _sink(tmp_path, F2, F1)
    assert sink.changed("src.e") and not sink.changed("src.m")
    assert _sink(tmp_path, F1, NEVER).changed("src.m")  # not covered: a bootstrap reads in full

    p3 = prod.handle(F3)
    p3.append_table("e", p3.con.sql("SELECT 3 AS id WHERE false"))
    p3.merge_table("m", p3.con.sql("SELECT 1 AS id, 'b' AS v"), pk="id")
    prod.publish(F3)
    sink3 = _sink(tmp_path, F3, F2)
    assert not sink3.changed("src.e") and sink3.changed("src.m")


def test_a_source_object_changed(tmp_path):
    prod = Producer(tmp_path)
    prod.handle(F1).write_object("model", b"abc")
    prod.publish(F1)
    prod.handle(F2).write_object("model", b"abc", if_changed=True)
    prod.publish(F2)
    assert not _sink(tmp_path, F2, F1).changed("src.model")
    assert _sink(tmp_path, F1, NEVER).changed("src.model")
    prod.handle(F3).write_object("model", b"xyz", if_changed=True)
    prod.publish(F3)
    assert _sink(tmp_path, F3, F2).changed("src.model")


def test_an_unpublished_source_asset_is_missing(tmp_path):
    prod = Producer(tmp_path)
    p = prod.handle(F1)
    p.write_table("t", _snapshot(p, 1))
    prod.publish(F1)
    with pytest.raises(MissingSourceAsset):
        _sink(tmp_path, F2, F1).changed("src.nope")
