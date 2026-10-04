"""A Spout or alert destination held entirely in a variable or secret (``${env:DATABASE_URL}``,
``${secret:SLACK_WEBHOOK}``): accepted when added, resolved and scheme-checked at delivery or test time,
and never echoed in an error."""
from __future__ import annotations

import duckdb
import pytest

from duckstring.alerts import NotifierError, get_notifier, parse_notifier_destination
from duckstring.egress import credentials
from duckstring.egress.base import get_egress
from duckstring.egress.destination import DestinationError, parse_destination


@pytest.fixture
def secrets():
    store: dict[str, str] = {}
    credentials.set_secret_provider(store.get)
    yield store
    credentials.set_secret_provider(None)


def test_whole_reference_detection():
    assert credentials.whole_reference("${env:DATABASE_URL}")
    assert credentials.whole_reference(" ${secret:SLACK} ")
    assert not credentials.whole_reference("postgres://u:${env:PW}@h/db")
    assert not credentials.whole_reference("${env:A}${env:B}")


def test_spout_destination_is_accepted_unresolved():
    dest = parse_destination("${env:DATABASE_URL}")
    assert dest.scheme is None and dest.raw == "${env:DATABASE_URL}"
    assert not dest.transactional  # unknown until resolved; the driver enforces a primary key at delivery


def test_spout_destination_resolves_to_its_driver(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://u:hunter2@db.internal/app?schema=analytics")
    driver = get_egress("${env:DATABASE_URL}")
    assert type(driver).__name__ == "PostgresEgressDriver"
    assert driver._schema() == "analytics"  # read from the resolved URI
    assert driver.dest.raw == "${env:DATABASE_URL}"  # the reference, not the value, is what's kept


def test_spout_destination_with_an_unsupported_resolved_scheme_hides_the_value(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "mysql://u:hunter2@db/app")
    with pytest.raises(DestinationError) as exc:
        get_egress("${env:DATABASE_URL}")
    assert "${env:DATABASE_URL}" in str(exc.value)
    assert "hunter2" not in str(exc.value) and "mysql" not in str(exc.value)


def test_spout_destination_missing_variable(monkeypatch):
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    with pytest.raises(DestinationError, match="NOT_SET_ANYWHERE"):
        get_egress("${env:NOT_SET_ANYWHERE}")


def test_file_spout_from_a_secret_writes(tmp_path, secrets):
    secrets["EXPORT_DIR"] = f"file://{tmp_path / 'exports'}"
    driver = get_egress("${secret:EXPORT_DIR}")
    con = duckdb.connect()
    from datetime import datetime, timezone

    driver.write_full(con, con.sql("SELECT 1 AS id"), table="t", pk=None, f=datetime.now(timezone.utc))
    assert (tmp_path / "exports" / "t.parquet").exists()


def test_driver_add_spout_accepts_a_whole_reference(tmp_path):
    from tests.test_spout import _driver  # the Spout suite's Catchment fixture

    d = _driver(tmp_path)
    assert d.add_spout("sales@1", None, None, "${env:DATABASE_URL}", "auto") == "spout"
    assert d.list_spouts("sales@1")[0]["destination"] == "${env:DATABASE_URL}"


def test_alert_destination_from_a_secret(secrets):
    assert parse_notifier_destination("${secret:SLACK_WEBHOOK}").scheme is None
    secrets["SLACK_WEBHOOK"] = "https://hooks.slack.com/services/T0/B0/token"
    assert type(get_notifier("${secret:SLACK_WEBHOOK}")).__name__ == "WebhookNotifier"


def test_email_alert_destination_from_a_secret(secrets):
    secrets["ONCALL"] = "mailto:oncall@example.com,data@example.com?smtp=smtp.example.com:587"
    notifier = get_notifier("${secret:ONCALL}")
    assert notifier.recipients == ["oncall@example.com", "data@example.com"]


def test_alert_destination_with_an_unsupported_resolved_scheme_hides_the_value(secrets):
    secrets["HOOK"] = "ftp://secret-host/drop"
    with pytest.raises(NotifierError) as exc:
        get_notifier("${secret:HOOK}")
    assert "${secret:HOOK}" in str(exc.value) and "secret-host" not in str(exc.value)
