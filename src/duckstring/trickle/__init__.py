"""``duckstring.trickle`` is the Trickle engine, published separately as ``duckstring-trickle`` (import
``trickle``; plans/trickle-extraction.md). This module makes the old name the same package: it registers
``trickle`` and each of its submodules under ``duckstring.trickle``, so ``duckstring.trickle.io is
trickle.io`` and existing imports, ``isinstance`` checks and monkeypatches keep working. New code imports
``trickle``."""

from __future__ import annotations

import importlib
import pkgutil
import sys

import trickle as _trickle

for _info in pkgutil.iter_modules(_trickle.__path__):
    sys.modules[f"{__name__}.{_info.name}"] = importlib.import_module(f"trickle.{_info.name}")
sys.modules[__name__] = _trickle
