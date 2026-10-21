"""`duckstring control VERB PONDS…`: act directly on Ponds' execution.

Every verb but `reset-contract` and `failure-budget` takes any number of Ponds (as name, or name@major) and the
selection helpers --all, --tree, --between and --downstream, which widen the set the way the web UI's Pond
Actions do::

    duckstring control kill reports
    duckstring control rebuild --all
    duckstring control repair sales --downstream
    duckstring control sleep transactions products
"""

from __future__ import annotations

from typing import Optional

import typer

from .refresh import _SILENT_HELP, _WATCH_HELP
from .refresh import send_and_watch as _post_trigger

_CATCHMENT = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted).")
_MAJOR = typer.Option(None, "--major", "-m", help="Major version to target (default: latest).")
_VERSION = typer.Option(None, "--version", "-v", help="Specific semver to target, e.g. 1.2.3.")
_PONDS = typer.Argument(None, metavar="[POND]...", help="Ponds, as name or name@major.")
_ALL = typer.Option(False, "--all", help="Every Pond in the Catchment.")
_TREE = typer.Option(False, "--tree", help="Add every Pond connected to the ones named.")
_BETWEEN = typer.Option(False, "--between", help="Add every Pond on a path between the ones named.")
_DOWNSTREAM = typer.Option(False, "--downstream", help="Add everything downstream of the ones named.")


def _cfg(catchment: Optional[str]) -> dict:
    from .config import resolve_catchment

    return resolve_catchment(catchment)[1]


def select(cfg: dict, ponds: Optional[list[str]], all_: bool, tree: bool, between: bool, downstream: bool,
           major: Optional[int] = None, version: Optional[str] = None) -> list[tuple[str, Optional[int], Optional[str]]]:
    """The Ponds a control verb acts on, as ``(name, major, version)``. One plain name with no helpers keeps
    --major/--version; anything wider resolves against the Catchment's status (a name means its highest major)."""
    ponds = ponds or []
    widened = all_ or tree or between or downstream
    if not widened and len(ponds) == 1 and "@" not in ponds[0]:
        return [(ponds[0], major, version)]
    if major is not None or version is not None:
        raise typer.BadParameter("--major and --version apply to one Pond: name several as name@major")
    if not ponds and not all_:
        raise typer.BadParameter("name at least one Pond, or pass --all")
    from . import _http

    status = _http.get(f"{cfg['url']}/api/status", auth=cfg).json()
    live = {p["id"]: p for p in status["ponds"] if not p.get("is_spout") and not p.get("is_draw")}
    if all_:
        scope = set(live)
    else:
        scope = {_resolve_seed(t, live) for t in ponds}
        parents: dict[str, set[str]] = {pid: set() for pid in live}
        children: dict[str, set[str]] = {pid: set() for pid in live}
        for src, sink in status["edges"]:
            if src in live and sink in live:
                children[src].add(sink)
                parents[sink].add(src)
        if downstream:
            scope |= _closure(scope, children)
        if between:
            scope |= (scope | _closure(scope, children)) & (scope | _closure(scope, parents))
        if tree:
            scope |= _closure(scope, {pid: children[pid] | parents[pid] for pid in live})
    return [(pid.rpartition("@")[0], int(pid.rpartition("@")[2]), None) for pid in sorted(scope)]


def _resolve_seed(token: str, live: dict[str, dict]) -> str:
    """A `name` or `name@major` token as a live Pond id (a name means its highest deployed major)."""
    if "@" in token:
        if token not in live:
            raise typer.BadParameter(f"no such Pond '{token}'")
        return token
    majors = sorted(int(p["major"]) for p in live.values() if p["name"] == token)
    if not majors:
        raise typer.BadParameter(f"no such Pond '{token}'")
    return f"{token}@{majors[-1]}"


def _closure(seeds: set[str], adj: dict[str, set[str]]) -> set[str]:
    """Everything reachable from ``seeds`` over ``adj``, the seeds excluded."""
    out: set[str] = set()
    stack = list(seeds)
    while stack:
        for nxt in adj.get(stack.pop(), ()):
            if nxt not in out and nxt not in seeds:
                out.add(nxt)
                stack.append(nxt)
    return out


def _label(name: str, major: Optional[int]) -> str:
    return name if major is None else f"{name}@{major}"


def _each(cfg: dict, targets, action: str, body: Optional[dict] = None, extra: Optional[dict] = None) -> None:
    from . import _http

    for name, major, version in targets:
        _http.post(f"{cfg['url']}/api/ponds/{name}/{action}", auth=cfg,
                   params={**_http.pond_params(major, version), **(extra or {})}, json=body or {})


def wake(
    ponds: Optional[list[str]] = _PONDS,
    all_: bool = _ALL, tree: bool = _TREE, between: bool = _BETWEEN, downstream: bool = _DOWNSTREAM,
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
    silent: bool = typer.Option(False, "--silent", help=_SILENT_HELP),
    watch: bool = typer.Option(False, "--watch", help=_WATCH_HELP),
) -> None:
    """Run each Pond once if its Sources already have newer data, without asking them to run."""
    cfg = _cfg(catchment)
    targets = select(cfg, ponds, all_, tree, between, downstream, major, version)
    if len(targets) == 1:
        name, m, v = targets[0]
        _post_trigger(cfg, name, m, v, silent, watch, "wake", {}, "Woken.", one_shot=True)
        return
    _each(cfg, targets, "wake")
    typer.echo(f"Woke {len(targets)} Ponds.")


def force(
    ponds: Optional[list[str]] = _PONDS,
    all_: bool = _ALL, tree: bool = _TREE, between: bool = _BETWEEN, downstream: bool = _DOWNSTREAM,
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
    silent: bool = typer.Option(False, "--silent", help=_SILENT_HELP),
    watch: bool = typer.Option(False, "--watch", help=_WATCH_HELP),
) -> None:
    """Rerun each Pond now at its current freshness, even with no upstream change, e.g. after deploying a fix.

    Freshness doesn't change, so Ponds downstream don't rerun because of it.
    """
    cfg = _cfg(catchment)
    targets = select(cfg, ponds, all_, tree, between, downstream, major, version)
    if len(targets) == 1:
        name, m, v = targets[0]
        _post_trigger(cfg, name, m, v, silent, watch, "force", {}, "Forced.", one_shot=True)
        return
    _each(cfg, targets, "force")
    typer.echo(f"Forced {len(targets)} Ponds.")


def rebuild(
    ponds: Optional[list[str]] = _PONDS,
    all_: bool = _ALL, tree: bool = _TREE, between: bool = _BETWEEN, downstream: bool = _DOWNSTREAM,
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
    clear: bool = typer.Option(False, "--clear", help="Remove a pending rebuild instead."),
) -> None:
    """Rebuild each Pond from scratch on its next run.

    Its working database is dropped and every Source is read in full, so Ponds downstream also read its
    Trickles in full. Nothing runs now. For an immediate rebuild, use `control repair`.
    """
    cfg = _cfg(catchment)
    targets = select(cfg, ponds, all_, tree, between, downstream, major, version)
    _each(cfg, targets, "rebuild", extra={"clear": clear})
    for name, m, _ in targets:
        label = _label(name, m)
        typer.echo(f"Rebuild cleared on '{label}'." if clear else f"'{label}' will rebuild on its next run.")


def reset(
    ponds: Optional[list[str]] = _PONDS,
    all_: bool = _ALL, tree: bool = _TREE, between: bool = _BETWEEN, downstream: bool = _DOWNSTREAM,
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
    clear_history: bool = typer.Option(False, "--clear-history", help="Also delete the run history."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Return each Pond to its freshly deployed state.

    Deletes its published data, working database and run ledger, and resets its freshness. Keeps its code,
    configuration and demand, so it rebuilds from scratch the next time it runs. The Ponds must be idle.
    """
    cfg = _cfg(catchment)
    targets = select(cfg, ponds, all_, tree, between, downstream, major, version)
    names = ", ".join(_label(n, m) for n, m, _ in targets)
    if not yes:
        typer.confirm(f"Reset {names} (delete data and state; keep code, configuration and demand)?", abort=True)
    _each(cfg, targets, "reset", extra={"clear_history": clear_history})
    typer.echo(f"Reset {names}. Each rebuilds from scratch when next refreshed.")


def wipe(
    ponds: Optional[list[str]] = _PONDS,
    all_: bool = _ALL, tree: bool = _TREE, between: bool = _BETWEEN, downstream: bool = _DOWNSTREAM,
    catchment: Optional[str] = _CATCHMENT,
    confirm: Optional[str] = typer.Option(None, "--confirm", help="The Catchment's name, to confirm without a prompt."),
) -> None:
    """Delete each Pond's run history, leaving its data alone.

    It can't be undone, so it asks for the Catchment's name (or takes it from --confirm).
    """
    from . import _http

    cfg = _cfg(catchment)
    targets = select(cfg, ponds, all_, tree, between, downstream)
    if confirm is None:
        name = (_http.get(f"{cfg['url']}/api/status", auth=cfg).json().get("catchment") or {}).get("name") or "confirm"
        confirm = typer.prompt(f"Type the Catchment's name ('{name}') to delete the run history of "
                               f"{len(targets)} Pond(s)")
    body = {"ponds": [{"name": n, "major": m} for n, m, _ in targets], "operations": ["wipe"], "confirm": confirm}
    resp = _http.post(f"{cfg['url']}/api/ponds/batch", auth=cfg, json=body).json()
    typer.echo(f"Wiped the run history of {len(targets)} Pond(s).")
    for err in resp.get("errors", []):
        typer.echo(f"  ! {err['pond']}: {err['error']}", err=True)


def repair(
    ponds: Optional[list[str]] = _PONDS,
    all_: bool = _ALL, tree: bool = _TREE, between: bool = _BETWEEN, downstream: bool = _DOWNSTREAM,
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
) -> None:
    """Rebuild a set of Ponds now, in dependency order, each reading its parents' rebuilt output.

    The set must be connected: a Pond linking two of them must be included too (--between adds it).
    """
    from . import _http

    cfg = _cfg(catchment)
    targets = select(cfg, ponds, all_, tree, between, False, major)
    body = {"ponds": [{"name": n, "major": m} for n, m, _ in targets], "downstream": downstream}
    order = _http.post(f"{cfg['url']}/api/repair", auth=cfg, json=body).json().get("scope", [])
    typer.echo(f"Repairing {len(order)} Pond(s) in order: {' → '.join(order)}")


def sleep(
    ponds: Optional[list[str]] = _PONDS,
    all_: bool = _ALL, tree: bool = _TREE, between: bool = _BETWEEN, downstream: bool = _DOWNSTREAM,
    catchment: Optional[str] = _CATCHMENT,
    upstream: bool = typer.Option(False, "--upstream", help="Also sleep every Pond upstream."),
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
) -> None:
    """Clear each Pond's demand and remove its recurring Refreshes. Runs in progress finish."""
    cfg = _cfg(catchment)
    targets = select(cfg, ponds, all_, tree, between, downstream, major, version)
    _each(cfg, targets, "sleep", body={"upstream": upstream})
    typer.echo(f"Asleep: {', '.join(_label(n, m) for n, m, _ in targets)}" + (" (and upstream)." if upstream else "."))


def kill(
    ponds: Optional[list[str]] = _PONDS,
    all_: bool = _ALL, tree: bool = _TREE, between: bool = _BETWEEN, downstream: bool = _DOWNSTREAM,
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
) -> None:
    """Stop each Pond's Duck immediately, abandoning its current run.

    A Pond stays killed, with no runs or retries, until it's woken, forced or cleared.
    """
    cfg = _cfg(catchment)
    targets = select(cfg, ponds, all_, tree, between, downstream, major, version)
    _each(cfg, targets, "kill")
    typer.echo(f"Killed {', '.join(_label(n, m) for n, m, _ in targets)}.")


def clear(
    ponds: Optional[list[str]] = _PONDS,
    all_: bool = _ALL, tree: bool = _TREE, between: bool = _BETWEEN, downstream: bool = _DOWNSTREAM,
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
) -> None:
    """Reset each failed or killed Pond without running it, and unblock the Ponds downstream."""
    cfg = _cfg(catchment)
    targets = select(cfg, ponds, all_, tree, between, downstream, major, version)
    _each(cfg, targets, "clear")
    typer.echo(f"Cleared {', '.join(_label(n, m) for n, m, _ in targets)}.")


def reset_contract(
    pond: str = typer.Argument(..., help="The Pond whose recorded output schema to forget."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
) -> None:
    """Forget the output schema recorded for this major line, and clear the failure.

    The next successful run records the schema again. Use it when a run failed for narrowing a column's
    type and you accept the change within the same major version. Ponds downstream may rely on the old
    schema, so it asks for confirmation.
    """
    from . import _http

    cfg = _cfg(catchment)
    if not yes:
        typer.confirm(
            f"Drop the recorded output schema for '{pond}'? A Sink pinned to this major line loses the "
            "guarantee that its columns keep their types.",
            abort=True,
        )
    r = _http.post(
        f"{cfg['url']}/api/ponds/{pond}/reset-contract", auth=cfg,
        params=_http.pond_params(major, version), json={},
    ).json()
    typer.echo(f"Contract reset for '{pond}' ({r.get('columns_cleared', 0)} recorded columns dropped); "
               "the next accepted run re-freezes it.")


def failure_budget(
    pond: str = typer.Argument(..., help="The Pond whose retry budgets to show or set."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
    immediate: Optional[int] = typer.Option(
        None, "--immediate", "-i", help="Retries of a failed Ripple within the same Pond Run."
    ),
    on_change: Optional[int] = typer.Option(
        None, "--on-change", "-o", help="Retries of a failed Pond Run when a Source next updates."
    ),
) -> None:
    """Show a Pond's retry budgets, or set them. They replace the values from pond.toml."""
    from . import _http

    cfg = _cfg(catchment)
    params = _http.pond_params(major, version)
    cur = _http.get(f"{cfg['url']}/api/ponds/{pond}/budget", auth=cfg, params=params).json()
    if immediate is None and on_change is None:
        typer.echo(f"immediate: {cur['immediate_retries']}   on-change: {cur['source_retries']}")
        return
    imm = cur["immediate_retries"] if immediate is None else immediate
    onc = cur["source_retries"] if on_change is None else on_change
    if imm < 0 or onc < 0:
        typer.echo("Error: budgets must be non-negative.", err=True)
        raise typer.Exit(1)
    _http.post(
        f"{cfg['url']}/api/ponds/{pond}/budget", auth=cfg, params=params,
        json={"immediate_retries": imm, "source_retries": onc},
    )
    typer.echo(f"Set '{pond}' — immediate: {imm}   on-change: {onc}")
