"""Backwards-compatible alias — aggregate metric specs now live in :mod:`duckstring.trickle.agg`.

Kept so ``from duckstring import agg`` continues to work. See :mod:`duckstring.trickle_io` for the rationale.
"""

from __future__ import annotations

from .trickle import agg as _agg


def __getattr__(name):  # PEP 562: forward all names to the relocated module
    return getattr(_agg, name)
