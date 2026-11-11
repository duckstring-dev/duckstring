"""What the VM backends without a baked image share (Azure VMs, Compute Engine): the boot script that installs
Duckstring with uv and runs a Duck or Pool agent, unique VM names, and the watcher that deletes a VM once its
Duck has exited (a clean exit powers the OS off, which on Azure still bills the VM and on Compute Engine still
bills its disk)."""

from __future__ import annotations

import logging
import re
import secrets
import shlex
import time

log = logging.getLogger("duckstring.vm")

REMOTE_ROOT = "/var/lib/duckstring"
WATCH_POLL_S = 20.0
WATCH_FOR_S = 6 * 3600.0  # how long a released VM is watched for its Duck to exit


def vm_name(stem: str) -> str:
    """A unique VM name from a pond key or pool (lower-case letters, digits and hyphens, starting with a letter,
    at most 50 characters: inside every cloud's limit)."""
    stem = re.sub(r"[^a-z0-9-]+", "-", stem.lower()).strip("-")[:36]
    return f"duck-{stem}-{secrets.token_hex(3)}"


def default_pip_spec(extra: str) -> str:
    from .. import __version__

    return f"duckstring[{extra}]=={__version__}"


def boot_script(args: list[str], pip_spec: str) -> str:
    """The startup script: install Duckstring into its own environment with uv (Ubuntu's system Python refuses
    ``pip install``), run the module, and power off when it exits cleanly. Output goes to the serial console,
    which each cloud keeps for ``diagnose``."""
    run = " ".join(shlex.quote(a) for a in ["/opt/duckstring/bin/python", "-m", *args])
    lines = [
        "#!/bin/bash",
        "set -euxo pipefail",
        "exec > >(tee -a /dev/console /var/log/duckstring-boot.log) 2>&1",
        f"mkdir -p {REMOTE_ROOT}",
        f"export HOME=/root UV_CACHE_DIR={REMOTE_ROOT}/uv-cache",
        "curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh",
        "uv venv /opt/duckstring --python 3.12",
        f"uv pip install --python /opt/duckstring/bin/python {shlex.quote(pip_spec)}",
        f"{run} && shutdown -h now",
    ]
    return "\n".join(lines) + "\n"


def delete_when_stopped(launcher, address, *, poll_s: float = WATCH_POLL_S) -> None:
    """Watch a released VM until its Duck has exited (``launcher.vm_live`` turns false), then delete it; a VM
    still up after ``WATCH_FOR_S`` is deleted anyway."""
    deadline = time.monotonic() + WATCH_FOR_S
    while time.monotonic() < deadline:
        try:
            state = launcher.vm_state(address)
            if state == "gone":
                return
            if not launcher.vm_live(address):
                launcher.delete_vm(address)
                return
        except Exception:
            log.debug("%s: state check failed for %s", launcher.name, address[-1], exc_info=True)
        time.sleep(poll_s)
    log.warning("%s: %s still running %d h after its Duck was released; deleting it",
                launcher.name, address[-1], WATCH_FOR_S // 3600)
    launcher.delete_vm(address)
