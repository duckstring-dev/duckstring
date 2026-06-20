"""Backwards-compatible alias — the Trickle builder now lives in :mod:`duckstring.trickle.builder`.

See :mod:`duckstring.trickle_io` for the rationale. New code should import from ``duckstring.trickle`` (or
``duckstring.trickle.builder``) directly.
"""

from __future__ import annotations

from .trickle import builder as _builder


def __getattr__(name):  # PEP 562: forward all names to the relocated module
    return getattr(_builder, name)
