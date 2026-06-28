"""Env-var-first credential resolution for egress destinations (see plans/egress.md "Secrets")."""

from __future__ import annotations

import pytest

from duckstring.egress import credentials as cred
from duckstring.egress.credentials import CredentialError, Reference


def test_resolve_env_reference():
    out = cred.resolve("postgres://u:${env:PGPASS}@host/db", env={"PGPASS": "s3cr3t"})
    assert out == "postgres://u:s3cr3t@host/db"


def test_resolve_multiple_and_repeated():
    env = {"A": "x", "B": "y"}
    assert cred.resolve("${env:A}-${env:B}-${env:A}", env=env) == "x-y-x"


def test_resolve_no_reference_is_identity():
    assert cred.resolve("s3://bucket/prefix", env={}) == "s3://bucket/prefix"


def test_missing_env_var_raises_naming_the_var_not_the_value():
    with pytest.raises(CredentialError) as exc:
        cred.resolve("${env:PGPASS}", env={})
    assert "PGPASS" in str(exc.value)


def test_unrelated_brace_syntax_passes_through():
    # Only env/secret schemes are interpolated; a bare ${HOME} or $FOO is left untouched.
    assert cred.resolve("${HOME}/$FOO/path", env={}) == "${HOME}/$FOO/path"


def test_secret_scheme_is_reserved_not_yet_supported():
    with pytest.raises(CredentialError) as exc:
        cred.resolve("${secret:PGPASS}", env={})
    assert "not yet supported" in str(exc.value)


def test_empty_reference_raises():
    with pytest.raises(CredentialError):
        cred.resolve("${env:}", env={})


def test_uses_process_environment_by_default(monkeypatch):
    monkeypatch.setenv("DS_TEST_CRED", "from-os-environ")
    assert cred.resolve("${env:DS_TEST_CRED}") == "from-os-environ"


# ─── references (pre-flight, never resolves) ─────────────────────────────────


def test_references_lists_in_order_with_scheme():
    refs = cred.references("${env:A}://${secret:B}/${env:A}")
    assert refs == [Reference("env", "A"), Reference("secret", "B"), Reference("env", "A")]


def test_references_is_safe_on_unresolvable():
    # No env lookup happens — naming the needs of a Spout doesn't require the secrets to be present.
    assert cred.references("${env:NOT_SET}") == [Reference("env", "NOT_SET")]


def test_references_empty_when_none():
    assert cred.references("postgres://host/db") == []
