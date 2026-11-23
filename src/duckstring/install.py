"""Machine setup a pip install can't do: ``duckstring install`` (plans/records-and-media.md).

Each component reports its state and, when missing, the steps that would install it. Steps come only from
official sources (the PostgreSQL project's own package repositories, Homebrew, DuckDB's extension
repository, the package index Duckstring came from) and this module never downloads a binary to run. It never
calls sudo: a step needing root says so, and the caller runs the command as root.

Kept free of DuckDB imports: the image build runs it under emulation for the other architecture, where
DuckDB doesn't finish importing.
"""

from __future__ import annotations

import gzip
import importlib.metadata
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

POSTGRES_MAJOR = 18
EXTRAS = ("aws", "azure", "gcp", "databricks", "dbt", "lineage", "starrocks")
POSTGRES_DOWNLOAD = "https://www.postgresql.org/download/"


@dataclass
class Step:
    """One thing to do: a command (``argv``) or an action of this module (``action``), described for the
    confirmation prompt."""

    describe: str
    argv: list[str] | None = None
    action: Callable[[], None] | None = None
    root: bool = False  # needs root on this machine


@dataclass
class Plan:
    component: str
    state: str  # "installed", "missing", "unsupported"
    detail: str = ""
    steps: list[Step] = field(default_factory=list)
    after: Callable[[], str] | None = None  # run once the steps succeed; returns a line to print


# ── Postgres ───────────────────────────────────────────────────────────────────────────────────────────


def _pg_major(bin_dir: Path) -> int | None:
    """The PostgreSQL major of the server in ``bin_dir``, or None when there's none."""
    if not (bin_dir / "pg_ctl").is_file() or not (bin_dir / "postgres").is_file():
        return None
    try:
        out = subprocess.run([str(bin_dir / "pg_ctl"), "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)(?:\.\d+)?", out.stdout)
    return int(m.group(1)) if m else None


def _pg_candidates(major: int | None) -> list[Path]:
    """Where a server might be: the configured place first, then where each official package puts it (stable
    paths, and Debian's and PGDG's RPMs keep the server binaries off the PATH), then the PATH."""
    found: list[Path] = []
    env = os.environ.get("DUCKSTRING_PG_BIN")
    if env:
        found.append(Path(env).expanduser())
    recorded = _recorded_pg_bin()
    if recorded:
        found.append(recorded)
    majors = [major] if major else list(range(POSTGRES_MAJOR + 2, 13, -1))
    for v in majors:
        found += [
            Path(f"/opt/homebrew/opt/postgresql@{v}/bin"),
            Path(f"/usr/local/opt/postgresql@{v}/bin"),
            Path(f"/Applications/Postgres.app/Contents/Versions/{v}/bin"),
            Path(f"/usr/lib/postgresql/{v}/bin"),
            Path(f"/usr/pgsql-{v}/bin"),
        ]
    # Last, and unresolved: a PATH entry is often a symlink into a versioned directory an upgrade removes.
    on_path = shutil.which("pg_ctl")
    if on_path:
        found.append(Path(on_path).parent)
    seen: set[Path] = set()
    unique = []
    for p in found:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def find_postgres(major: int | None = None) -> tuple[Path, int] | None:
    """A PostgreSQL server's bin directory and its major: of ``major`` exactly when given, else the first
    found in the order ``DUCKSTRING_PG_BIN``, the recorded install, the official packages' places (newest
    major first), the PATH."""
    for bin_dir in _pg_candidates(major):
        got = _pg_major(bin_dir)
        if got is not None and (major is None or got == major):
            return bin_dir, got
    return None


def _recorded_pg_bin() -> Path | None:
    try:
        from duckstring.cli.config import load_config

        value = load_config().get("install", {}).get("postgres")
    except Exception:
        return None
    return Path(value) if value else None


def _record_pg_bin(bin_dir: Path) -> None:
    from duckstring.cli.config import load_config, save_config

    config = load_config()
    config.setdefault("install", {})["postgres"] = str(bin_dir)
    save_config(config)


def _os_release() -> dict[str, str]:
    try:
        text = Path("/etc/os-release").read_text()
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v.strip().strip('"')
    return out


def _family(release: dict[str, str]) -> str:
    ids = {release.get("ID", "")} | set(release.get("ID_LIKE", "").split())
    if release.get("ID") == "amzn":
        return "amazon"
    if ids & {"debian", "ubuntu"}:
        return "debian"
    if release.get("ID") == "fedora":
        return "fedora"
    if ids & {"rhel", "centos", "rocky", "almalinux"}:
        return "rhel"
    return "other"


def postgres_plan(major: int = POSTGRES_MAJOR) -> Plan:
    """Find a server of ``major``, else the official packages' install for this machine."""

    def record() -> str:
        found = find_postgres(major)
        if found is None:
            raise InstallError(f"PostgreSQL {major} was installed but its pg_ctl wasn't found; set DUCKSTRING_PG_BIN")
        try:
            _record_pg_bin(found[0])
        except Exception:  # pragma: no cover - a read-only home still has the server, found by place
            pass
        return f"postgres: PostgreSQL {major} at {found[0]}"

    found = find_postgres(major)
    if found:
        return Plan("postgres", "installed", f"PostgreSQL {found[1]} at {found[0]}", after=record)
    system = platform.system()
    plan = Plan("postgres", "missing", f"PostgreSQL {major}", after=record)
    if system == "Darwin":
        brew = shutil.which("brew")
        if not brew:
            return Plan("postgres", "unsupported",
                        f"install Homebrew (https://brew.sh) or Postgres.app, then run this again ({POSTGRES_DOWNLOAD})")
        plan.steps.append(Step(f"brew install postgresql@{major}", [brew, "install", f"postgresql@{major}"]))
        return plan
    if system == "Linux":
        family = _family(_os_release())
        if family == "debian":
            # The PostgreSQL project's apt repository (PGDG), added by the distribution's own postgresql-common
            # with the signing key it ships. Debian's packaging creates and starts a "main" cluster on install;
            # Duckstring runs its own, so that's turned off before the server is installed.
            conf = "/etc/postgresql-common/createcluster.d/duckstring.conf"
            plan.steps += [
                Step("apt-get update", ["apt-get", "update"], root=True),
                Step("apt-get install postgresql-common (the PostgreSQL project's repository setup)",
                     ["apt-get", "install", "-y", "--no-install-recommends", "postgresql-common", "ca-certificates"],
                     root=True),
                Step("add the PostgreSQL apt repository (apt.postgresql.org)",
                     ["/usr/share/postgresql-common/pgdg/apt.postgresql.org.sh", "-y"], root=True),
                Step(f"turn off the default cluster ({conf})",
                     ["sh", "-c", f"mkdir -p {os.path.dirname(conf)} && echo 'create_main_cluster = false' > {conf}"],
                     root=True),
                Step(f"apt-get install postgresql-{major}",
                     ["apt-get", "install", "-y", "--no-install-recommends", f"postgresql-{major}"], root=True),
            ]
            return plan
        if family == "rhel":
            el = _os_release().get("VERSION_ID", "9").split(".")[0]
            arch = platform.machine()
            repo = (f"https://download.postgresql.org/pub/repos/yum/reporpms/EL-{el}-{arch}/"
                    "pgdg-redhat-repo-latest.noarch.rpm")
            plan.steps += [
                Step("add the PostgreSQL yum repository (download.postgresql.org)", ["dnf", "install", "-y", repo],
                     root=True),
                Step("disable the distribution's postgresql module",
                     ["sh", "-c", "dnf -qy module disable postgresql || true"], root=True),
                Step(f"dnf install postgresql{major}-server", ["dnf", "install", "-y", f"postgresql{major}-server"],
                     root=True),
            ]
            return plan
        return Plan("postgres", "unsupported",
                    f"no automatic install for this distribution: install PostgreSQL {major} from {POSTGRES_DOWNLOAD}, "
                    "then set DUCKSTRING_PG_BIN to its bin directory")
    return Plan("postgres", "unsupported",
                f"install PostgreSQL {major} with the installer from {POSTGRES_DOWNLOAD}, "
                "then set DUCKSTRING_PG_BIN to its bin directory")


# ── DuckDB extensions ──────────────────────────────────────────────────────────────────────────────────


def _duckdb_platform() -> str:
    system, machine = platform.system(), platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if arch is None:
        raise InstallError(f"no DuckDB extensions are published for {machine}")
    prefix = {"Linux": "linux", "Darwin": "osx", "Windows": "windows"}.get(system)
    if prefix is None:
        raise InstallError(f"no DuckDB extensions are published for {system}")
    return f"{prefix}_{arch}"


def default_extensions() -> list[str]:
    """The extensions Duckstring loads on this machine: object-store reads, DuckLake Spouts, Records reads,
    and Azure storage when the azure extra is present."""
    from importlib.util import find_spec

    names = ["httpfs", "ducklake", "postgres"]
    if find_spec("adlfs"):
        names.append("azure")
    return names


# DuckDB's aliases: the file is named by the canonical name (``INSTALL postgres`` writes postgres_scanner).
_EXTENSION_ALIASES = {"postgres": "postgres_scanner", "sqlite": "sqlite_scanner", "sqlite3": "sqlite_scanner",
                      "mysql": "mysql_scanner", "http": "httpfs", "https": "httpfs", "s3": "httpfs"}


def extension_dir() -> Path:
    version = "v" + importlib.metadata.version("duckdb")
    return Path.home() / ".duckdb" / "extensions" / version / _duckdb_platform()


def _fetch_extension(name: str, target: Path) -> None:
    version = "v" + importlib.metadata.version("duckdb")
    url = f"https://extensions.duckdb.org/{version}/{_duckdb_platform()}/{name}.duckdb_extension.gz"
    # The CDN refuses urllib's default User-Agent (403); DuckDB's own and curl's are let through.
    request = urllib.request.Request(url, headers={"User-Agent": "duckstring-install"})
    with urllib.request.urlopen(request, timeout=120) as resp:
        data = gzip.decompress(resp.read())
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".partial")
    tmp.write_bytes(data)
    os.replace(tmp, target)


def extension_plans(names: list[str] | None = None) -> list[Plan]:
    directory = extension_dir()
    plans = []
    for name in names or default_extensions():
        if not re.fullmatch(r"[a-z0-9_]+", name):
            raise InstallError(f"not an extension name: {name!r}")
        file = _EXTENSION_ALIASES.get(name, name)
        target = directory / f"{file}.duckdb_extension"
        if target.is_file():
            plans.append(Plan(f"extension {name}", "installed", str(directory)))
            continue
        plans.append(Plan(f"extension {name}", "missing", "fetch from extensions.duckdb.org", steps=[
            Step(f"fetch {name} from extensions.duckdb.org into {directory}",
                 action=lambda file=file, target=target: _fetch_extension(file, target))
        ]))
    return plans


# ── Duckstring extras ──────────────────────────────────────────────────────────────────────────────────


def _extra_requirements(extra: str) -> list[str]:
    """The distribution names an extra of Duckstring requires."""
    try:
        requires = importlib.metadata.distribution("duckstring").requires or []
    except importlib.metadata.PackageNotFoundError:
        return []
    names = []
    for req in requires:
        if re.search(rf"""extra\s*==\s*['"]{re.escape(extra)}['"]""", req):
            m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", req)
            if m:
                names.append(m.group(1))
    return names


def _installed(dist: str) -> bool:
    try:
        importlib.metadata.distribution(dist)
        return True
    except importlib.metadata.PackageNotFoundError:
        return False


def extra_present(extra: str) -> bool:
    reqs = _extra_requirements(extra)
    return bool(reqs) and all(_installed(r) for r in reqs)


def extra_plan(extras: list[str]) -> Plan:
    """One ``uv pip install`` of every named extra missing here, pinned to the running Duckstring (its
    checkout, wheel, commit or released version), keeping the extras it already has."""
    from duckstring.environments import _requirement, _uv

    for extra in extras:
        if extra not in EXTRAS:
            raise InstallError(f"unknown component {extra!r} (components: postgres, extensions, {', '.join(EXTRAS)})")
    missing = [e for e in extras if not extra_present(e)]
    name = "extras " + ", ".join(extras)
    if not missing:
        return Plan(name, "installed", sys.prefix)
    keep = [e for e in EXTRAS if extra_present(e)]
    spec = _requirement("duckstring", sorted(set(keep) | set(missing)))
    argv = [_uv(), "pip", "install", "--python", sys.executable, *spec]
    return Plan(name, "missing", ", ".join(missing),
                steps=[Step("uv pip install " + " ".join(spec) + f" (into {sys.prefix})", argv)])


# ── Running a plan ─────────────────────────────────────────────────────────────────────────────────────


class InstallError(Exception):
    pass


def needs_root(plans: list[Plan]) -> bool:
    return any(s.root for p in plans for s in p.steps) and hasattr(os, "geteuid") and os.geteuid() != 0


def run(plans: list[Plan], echo: Callable[[str], None]) -> None:
    """Carry out every plan's steps in order, stopping at the first failure."""
    for plan in plans:
        for step in plan.steps:
            echo(f"→ {step.describe}")
            if step.argv is not None:
                result = subprocess.run(step.argv)
                if result.returncode != 0:
                    raise InstallError(f"{plan.component}: `{shlex.join(step.argv)}` exited {result.returncode}")
            elif step.action is not None:
                try:
                    step.action()
                except InstallError:
                    raise
                except Exception as exc:
                    raise InstallError(f"{plan.component}: {exc}") from exc
        if plan.after is not None and plan.state != "unsupported":
            echo(plan.after())
