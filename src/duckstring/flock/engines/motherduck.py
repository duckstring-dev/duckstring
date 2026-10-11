"""The MotherDuck Flock engine (plans/flock-motherduck.md): a job runs on DuckDB in MotherDuck.

MotherDuck is DuckDB, so the job's own SQL runs unchanged and everything the builder expresses in SQL is
eligible (see :mod:`.remote_duckdb` for how sources travel and results return).

**Sizes ("kicking up").** A Duckling's size is set per MotherDuck account (user or service account), and
changing it takes minutes, so size is chosen by choosing an account. The engine name may carry a profile:
``motherduck`` uses the secret ``MOTHERDUCK_TOKEN``, ``motherduck/giga`` uses ``MOTHERDUCK_TOKEN_GIGA``.
Point a heavy Pond at the large account with ``pond.toml [flock] engine = "motherduck/giga"`` or
``duckstring duck set POND --engine motherduck/giga``.

Config:

- the token: the secret ``MOTHERDUCK_TOKEN`` (or ``MOTHERDUCK_TOKEN_<PROFILE>``) from the Catchment's
  secret store, sent on the run's job; else MotherDuck's own ``motherduck_token`` environment variable
  (default profile only). Never on a command line.
- ``DUCKSTRING_FLOCK_MOTHERDUCK_DATABASE``: the MotherDuck database uploads go to (created if missing;
  default ``duckstring_flock``). Uploaded tables are dropped when their job ends.

For MotherDuck to read Sources in place from the data root, the account needs a secret for the bucket,
created once by the user (``CREATE SECRET IN MOTHERDUCK (TYPE S3, ...)``). Without one, a remote read fails
and the job runs locally, counted as a failed dispatch.
"""

from __future__ import annotations

from .remote_duckdb import RemoteDuckDBEngine, _q

_DEFAULT_DATABASE = "duckstring_flock"


def _profile(name: str | None) -> str:
    _, _, profile = (name or "motherduck").partition("/")
    return profile.strip()


class MotherDuckEngine(RemoteDuckDBEngine):
    SECRETS: tuple[str, ...] = ("MOTHERDUCK_TOKEN",)

    @classmethod
    def secrets_for(cls, name: str) -> tuple[str, ...]:
        """The token secret for engine ``name``: ``MOTHERDUCK_TOKEN``, or ``MOTHERDUCK_TOKEN_<PROFILE>``."""
        profile = _profile(name)
        return (f"MOTHERDUCK_TOKEN_{profile.upper()}",) if profile else cls.SECRETS

    def __init__(self, env, name: str | None = None):
        super().__init__(env, name or "motherduck")
        (secret,) = self.secrets_for(self.name)
        self._token = env.get(secret) or (None if _profile(self.name) else env.get("motherduck_token"))
        self.database = env.get("DUCKSTRING_FLOCK_MOTHERDUCK_DATABASE") or _DEFAULT_DATABASE

    def enabled(self) -> bool:
        return bool(self._token)

    def connect(self):
        import duckdb

        return duckdb.connect("md:", config={"motherduck_token": self._token})

    def scratch(self, remote) -> str:
        remote.execute(f"CREATE DATABASE IF NOT EXISTS {_q(self.database)}")
        return f"{_q(self.database)}.main"
