"""Command-line entry point for the computer-use automation system.

Phase 0/1 exposes only `serve-app`. Later phases add `discover`, `replay`,
`chaos`, and `handoff` commands.
"""

from __future__ import annotations

import typer
from dotenv import load_dotenv

load_dotenv()

app = typer.Typer(
    name="cua",
    help="Computer-use automation: discover, record, replay, and hand off.",
    no_args_is_help=True,
)


@app.command("serve-app")
def serve_app(
    host: str = typer.Option(None, help="Bind host (default: MOCK_APP_HOST or 127.0.0.1)"),
    port: int = typer.Option(None, help="Bind port (default: MOCK_APP_PORT or 8000)"),
    reload: bool = typer.Option(False, help="Auto-reload on code changes (dev only)"),
) -> None:
    """Run the mock legacy credit-union console (the automation target)."""
    from mock_app.__main__ import run

    run(host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
