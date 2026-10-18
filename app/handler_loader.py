"""One Telegram handler manifest for main.py, run.py and standalone bot.py."""
from importlib import import_module

HANDLER_MODULES = (
    'app.handlers.commands', 'app.handlers.peers', 'app.handlers.guard',
    'app.handlers.join_gate', 'app.handlers.groups', 'app.handlers.portable', 'app.handlers.restricted', 'app.handlers.bots',
    'app.handlers.br', 'app.handlers.reel', 'app.handlers.admin',
    'app.handlers.billing', 'app.handlers.extras', 'app.handlers.inline',
    'app.handlers.linkpanel', 'app.handlers.manage', 'app.handlers.messages',
    'app.handlers.panel_v3', 'app.handlers.user', 'app.handlers.command_fallback',
)


def load_handlers(client):
    from app import runtime
    runtime.set_client(client)
    for name in HANDLER_MODULES:
        import_module(name)
    runtime.install_handlers(client)
    return len(runtime.handler_registrations)
