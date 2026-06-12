from __future__ import annotations

import typer


def pond_params(major: int | None = None, version: str | None = None) -> dict:
    """Query params targeting one major line of a Pond (omitted = the server's default, the
    highest deployed major)."""
    params: dict = {}
    if major is not None:
        params["major"] = major
    if version is not None:
        params["version"] = version
    return params


def request(method: str, url: str, key: str | None = None, **kwargs):
    import httpx

    raw_timeout = kwargs.pop("timeout", None)
    if raw_timeout is None:
        timeout = httpx.Timeout(60.0, connect=5.0)
    elif isinstance(raw_timeout, (int, float)):
        timeout = httpx.Timeout(float(raw_timeout), connect=5.0)
    else:
        timeout = raw_timeout

    if key:
        headers = kwargs.pop("headers", None) or {}
        headers.setdefault("Authorization", f"Bearer {key}")
        kwargs["headers"] = headers

    try:
        resp = httpx.request(method, url, timeout=timeout, **kwargs)
        resp.raise_for_status()
        return resp
    except httpx.ConnectError:
        typer.echo(f"Error: could not connect to {url}", err=True)
        typer.echo("Is the Catchment running? Start it with: duckstring catchment start <name>", err=True)
        raise typer.Exit(1) from None
    except httpx.TimeoutException:
        typer.echo(f"Error: request to {url} timed out", err=True)
        raise typer.Exit(1) from None
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            typer.echo("Error: the Catchment rejected the API key (401).", err=True)
            typer.echo("Set one for this catchment: duckstring catchment connect --name <name> --path <url> --key <key>",
                       err=True)
        elif exc.response.status_code == 404:
            typer.echo(f"Error: endpoint not found — {exc.request.url}", err=True)
            try:
                detail = exc.response.json().get("detail")
                if detail:
                    typer.echo(f"  {detail}", err=True)
            except Exception:
                pass
        else:
            typer.echo(f"Error: {exc.response.status_code} from Catchment", err=True)
            try:
                detail = exc.response.json().get("detail", exc.response.text)
            except Exception:
                detail = exc.response.text[:300]
            typer.echo(f"  {detail}", err=True)
        raise typer.Exit(1) from None


def get(url: str, key: str | None = None, **kwargs):
    return request("GET", url, key=key, **kwargs)


def post(url: str, key: str | None = None, **kwargs):
    return request("POST", url, key=key, **kwargs)
