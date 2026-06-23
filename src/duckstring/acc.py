"""Backwards-compatible alias — scan (order-dependent) metric specs live in :mod:`duckstring.trickle.acc`.

Kept so ``from duckstring import acc`` works alongside ``from duckstring import agg``. See
:mod:`duckstring.trickle_io` for the rationale.
"""

from __future__ import annotations

from .trickle import acc as _acc


def __getattr__(name):  # PEP 562: forward all names to the relocated module
    return getattr(_acc, name)
