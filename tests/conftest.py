"""Shared fixtures: an in-process mock console served over real HTTP."""

from __future__ import annotations

import socket
import threading
import time
from types import SimpleNamespace

import pytest
import uvicorn

from mock_app.app import create_app
from mock_app.chaos import ChaosController
from mock_app.config import Settings
from mock_app.data import MemberStore

USER, PASSWORD = "teller01", "Pa55word!"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def mock_server():
    """Run the mock console with uvicorn in a background thread for browser tests."""
    settings = Settings(
        host="127.0.0.1", port=_free_port(), username=USER, password=PASSWORD, session_idle_seconds=0,
        restricted_members=frozenset({"40403"}), crashing_members=frozenset({"50500"}),
    )
    chaos = ChaosController()
    app = create_app(settings=settings, chaos=chaos, store=MemberStore())
    config = uvicorn.Config(app, host=settings.host, port=settings.port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "mock server failed to start"
    yield SimpleNamespace(base_url=f"http://{settings.host}:{settings.port}", chaos=chaos, settings=settings)
    server.should_exit = True
    thread.join(timeout=5)
