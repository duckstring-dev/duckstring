from __future__ import annotations

import typer


def request(method: str, url: str, **kwargs):
    import httpx

    raw_timeout = kwargs.pop("timeout", None)
    if raw_timeout is None:
        timeout = httpx.Timeout(60.0, connect=5.0)
    elif isinstance(raw_timeout, (int, float)):
        timeout = httpx.Timeout(float(raw_timeout), connect=5.0)
    else:
        timeout = raw_timeout

    try:
        resp = httpx.request(method, url, timeout=timeout, **kwargs)
        resp.raise_for_status()
        return resp
    except httpx.ConnectError:
        typer.echo(f"Error: could not connect to {url}", err=True)
        typer.echo("Is the Catchment running? Start it with: duckstring catchment start", err=True)
        raise typer.Exit(1) from None
    except httpx.TimeoutException:
        typer.echo(f"Error: request to {url} timed out", err=True)
        raise typer.Exit(1) from None
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            typer.echo(f"Error: endpoint not found — {exc.request.url}", err=True)
            typer.echo("This feature may not yet be implemented in the Catchment server.", err=True)
        else:
            typer.echo(f"Error: {exc.response.status_code} from Catchment", err=True)
            try:
                detail = exc.response.json().get("detail", exc.response.text)
            except Exception:
                detail = exc.response.text[:300]
            typer.echo(f"  {detail}", err=True)
        raise typer.Exit(1) from None


def get(url: str, **kwargs):
    return request("GET", url, **kwargs)


def post(url: str, **kwargs):
    return request("POST", url, **kwargs)
