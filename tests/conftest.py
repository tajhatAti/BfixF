"""Shared test setup.

Handler modules register callbacks on `runtime.bot` at import time, so tests get
a stub client — that way the whole suite runs offline, with no Telegram account.
"""
from __future__ import annotations

import pytest


class StubClient:
    """Minimal TelegramClient stand-in that records registered handlers."""

    def __init__(self) -> None:
        self.registered: list = []
        self.parse_mode = "html"
        self._connected = True

    # event registration -----------------------------------------------------
    def on(self, _event_builder=None, *_args, **_kwargs):
        def decorator(func):
            self.registered.append(func)
            return func
        return decorator

    def is_connected(self) -> bool:
        return self._connected

    # no-op API surface used by tests ---------------------------------------
    async def send_message(self, *_args, **_kwargs):
        return None

    async def send_file(self, *_args, **_kwargs):
        return None

    async def get_me(self):
        from types import SimpleNamespace
        return SimpleNamespace(id=1, username="test_bot", first_name="Test")

    async def get_messages(self, *_args, **_kwargs):
        return None

    async def get_input_entity(self, entity):
        return entity

    async def __call__(self, *_args, **_kwargs):
        return None


# Install the stub before any test module imports a handler (import order matters:
# handler modules call bot.on(...) at import time).
import app.runtime as runtime                                    # noqa: E402

if runtime.get_client() is None:
    runtime.set_client(StubClient())
runtime.bot_username = "test_bot"


@pytest.fixture(scope="session", autouse=True)
def stub_bot():
    """Import every handler module exactly once and hand back the stub client."""
    from importlib import import_module
    for name in ("app.handlers.router", "app.handlers.peers", "app.handlers.guard",
                 "app.handlers.portable",
                 "app.handlers.restricted", "app.handlers.br", "app.handlers.reel", "app.handlers.admin", "app.handlers.billing",
                 "app.handlers.extras", "app.handlers.inline", "app.handlers.linkpanel", "app.handlers.manage",
                 "app.handlers.messages", "app.handlers.panel_v3", "app.handlers.bots",
                 "app.handlers.user"):
        import_module(name)
    return runtime.get_client()
