"""Settings for the mock legacy console. All values are fake demo data."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    username: str
    password: str
    session_idle_seconds: int
    # Member numbers that always trigger a specific server-side condition.
    restricted_members: frozenset[str]   # -> permission denied
    crashing_members: frozenset[str]     # -> application error page


def load_settings() -> Settings:
    return Settings(
        host=os.getenv("MOCK_APP_HOST", "127.0.0.1"),
        port=int(os.getenv("MOCK_APP_PORT", "8000")),
        username=os.getenv("MOCK_APP_USERNAME", "teller01"),
        password=os.getenv("MOCK_APP_PASSWORD", "Pa55word!"),
        session_idle_seconds=int(os.getenv("MOCK_APP_SESSION_IDLE_SECONDS", "0")),
        restricted_members=frozenset({"40403"}),
        crashing_members=frozenset({"50500"}),
    )
