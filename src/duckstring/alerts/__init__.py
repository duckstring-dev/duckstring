"""Alerts — failure & freshness notifications to external channels. See plans/alerts.md.

The observability sibling of a Spout: operational config + a scheme-selected notifier seam +
``${env:}``/``${secret:}`` credentials + an async delivery worker that never cascades a failure back into
the engine. Alerting *observes* the state the engine already computes; it adds no orchestration state.
"""

from __future__ import annotations

from .base import Notifier, NotifierError, get_notifier, parse_notifier_destination
from .event import KNOWN_EVENTS, AlertEvent, normalise_events

__all__ = [
    "AlertEvent",
    "KNOWN_EVENTS",
    "Notifier",
    "NotifierError",
    "get_notifier",
    "normalise_events",
    "parse_notifier_destination",
]
