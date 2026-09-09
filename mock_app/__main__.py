"""`python -m mock_app` runs the mock legacy console with uvicorn."""

from __future__ import annotations

import uvicorn
from dotenv import load_dotenv

from .config import load_settings


def run(host: str | None = None, port: int | None = None, reload: bool = False, variant: str | None = None) -> None:
    load_dotenv()
    if variant:
        import os

        os.environ["MOCK_APP_VARIANT"] = variant
    settings = load_settings()
    uvicorn.run(
        "mock_app.app:app",
        host=host or settings.host,
        port=port or settings.port,
        reload=reload,
        log_level="info",
    )


if __name__ == "__main__":
    run()
