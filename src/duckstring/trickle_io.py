"""Backwards-compatible alias — Trickle I/O now lives in :mod:`duckstring.trickle.io`.

The incremental engine was pulled into the self-contained ``duckstring.trickle`` subpackage (ready to be
extracted into its own distribution — see ``CLAUDE.md``). This module forwards every attribute to the new
location so existing ``duckstring.trickle_io`` imports keep working; new code should import from
``duckstring.trickle`` (or ``duckstring.trickle.io``) directly.
"""

from __future__ import annotations

from .trickle import io as _io


def __getattr__(name):  # PEP 562: forward all names (incl. private helpers) to the relocated module
    return getattr(_io, name)
