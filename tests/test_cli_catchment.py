"""`duckstring catchment init/start` warn when an open Catchment binds beyond this machine."""

import pytest

from duckstring.cli.catchment import _is_loopback


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_binds_need_no_warning(host):
    assert _is_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.1.5", "catchment.internal"])
def test_other_binds_are_reachable_from_elsewhere(host):
    assert not _is_loopback(host)
