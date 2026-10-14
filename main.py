"""Entry point for hosting panels — the whole product in one process.

Panels look for an entry file in a fixed order (``main.py`` → ``app.py`` →
``bot.py`` → ``server.py`` → ``index.py`` → ``run.py``), so the name of this file
matters: it is the first candidate and therefore what CodeNest/RunSpace will run.

What it starts (see ``run.py``):

* 🌐 the website — public store front + the whole admin panel — bound to
  ``0.0.0.0`` and to ``$PORT`` (panels inject it; the live URL only appears once
  the app opens that port), and
* 🤖 the Telegram bot in the same process (it retries forever, so a network blip
  never takes the website down).

    python main.py

``python bot.py`` works the same way (it serves the website in a background
thread), and ``python run.py`` is the original combined entry point.
"""
from __future__ import annotations

from run import main

if __name__ == "__main__":
    raise SystemExit(main())
