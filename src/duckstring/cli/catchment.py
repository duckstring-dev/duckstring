from __future__ import annotations

import typer

app = typer.Typer(help="Work with Catchments.", add_completion=False, no_args_is_help=True)


@app.command()
def start(
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Host to bind to."),
    port: int = typer.Option(7474, "--port", "-p", help="Port to listen on."),
) -> None:
    """Start the Catchment web server."""
    import uvicorn
    from rich.console import Console
    from rich.panel import Panel

    console = Console()
    console.print(
        Panel(
            f"[bold white]duckstring catchment[/bold white]\n\n"
            f"  [dim]http://{host}:{port}[/dim]",
            border_style="bright_black",
            padding=(1, 2),
        )
    )
    uvicorn.run(
        "duckstring.catchment.app:app",
        host=host,
        port=port,
        reload=False,
        log_level="warning",
    )
