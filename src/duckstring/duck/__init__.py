"""The Duck: the per-Pond worker that executes Pond Runs (intra-Pond push).

A Duck is spawned by the Catchment against a Base Pond when a Pond Run is needed and none is running,
and shut down when the Pond returns to idle. It owns the push-only :mod:`duckstring.engine.worker`
engine + the run ledger (``ponds/{base_pond}/pond.db``), executes Ripple functions, and reports
progress to the Catchment. It keeps finishing in-flight runs even if the Catchment is unreachable,
buffering events and replaying them on reconnect (idempotent on freshness ``F``).
"""
