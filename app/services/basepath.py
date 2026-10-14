"""Running behind a path prefix — the hosting panel's live URL.

CodeNest / RunSpace (and Render-like panels in general) never expose a job on the
bare domain root. They publish

    https://<panel>/live/<job>/

and reverse-proxy everything under that prefix into the job's own port, adding

    X-Forwarded-Prefix: /live/<job>

Two things follow from that, and both used to be broken here:

1. **Links must keep the prefix.** A page served at ``/live/<job>/admin`` that
   contains ``href="/admin/files"`` sends the browser to the *panel's* root —
   a 404. The fix is small and universal: every page gets
   ``<base href="{prefix}/">`` and every root-absolute link is written without
   the leading slash, so ``href="admin/files"`` resolves through the base — with
   a prefix and without one (``<base href="/">``) alike.
2. **Redirects** (``Location: /admin/users``) are rewritten by the proxy itself,
   so they need nothing from us.

Nothing here ever trusts the header blindly: it must start with ``/``, cannot
contain ``..``, and is capped in length — a forged header can therefore only
move links around, never escape the prefix.
"""
from __future__ import annotations

import contextvars
import re

#: the current request's prefix ("" when the app is served from the domain root)
_prefix: contextvars.ContextVar[str] = contextvars.ContextVar("base_prefix", default="")

#: ``href="/admin"``, ``href='/admin'`` and the unquoted ``action=/admin`` alike
_ATTR = re.compile(
    r'(?P<attr>\b(?:href|action|src|poster|data-url|formaction)=)'
    r'(?P<quote>["\']?)/(?!/)')
#: literal URLs inside JavaScript — ``fetch('/admin/api/…')`` must not jump to
#: the panel root either, and `<base>` does not help for these.
_JS = re.compile(
    r'(?P<head>(?:\bfetch|\bopen)\s*\(\s*|'
    r'\blocation\.(?:href|assign|replace)\s*=\s*|'
    r'\b(?:window|document)\.location\s*=\s*)'
    r'(?P<quote>["\'])/(?!/)')


def normalize(value: str | None) -> str:
    """`"/live/abc/"` → `"/live/abc"`; anything unusable → `""`."""
    raw = (value or "").strip()
    if not raw or not raw.startswith("/") or ".." in raw:
        return ""
    raw = "/" + raw.strip("/")
    if len(raw) > 200 or len(raw.split("/")) > 6:
        return ""
    return "" if raw == "/" else raw


def set_prefix(value: str | None) -> str:
    """Remember the prefix for the current task (returns what was stored)."""
    clean = normalize(value)
    _prefix.set(clean)
    return clean


def current() -> str:
    return _prefix.get()


def url(path: str = "/") -> str:
    """An absolute-path URL that already carries the prefix."""
    return f"{current()}{path}" if path.startswith("/") else f"{current()}/{path}"


def relativize(html: str) -> str:
    """Make root-absolute links base-relative (pairs with ``<base href>``).

    Handles the three shapes that actually appear in the templates: quoted
    attributes in both quote styles, unquoted ones, and literal ``fetch('/…')``
    / ``location.href = '/…'`` strings inside the page's JavaScript.
    """
    html = _ATTR.sub(lambda m: f'{m.group("attr")}{m.group("quote")}', html)
    return _JS.sub(lambda m: f'{m.group("head")}{m.group("quote")}', html)
