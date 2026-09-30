"""Jinja2 setup for the web admin: filters for times and message text, and
globals every page needs (CSRF token, current path, runtime status)."""

from datetime import datetime
from pathlib import Path
import time

import jinja2
from fastapi import Request
from fastapi.templating import Jinja2Templates

from naruto.markers import message_body
from naruto.services import Services
from naruto.web.auth import csrf_token

TEMPLATE_DIR = Path(__file__).parent / "templates"


def flash(request: Request, message: str, kind: str = "ok") -> None:
    """Show ``message`` at the top of the next full page (kind: ok, warn, error)."""
    request.session.setdefault("flash", []).append([kind, message])


def make_templates(services: Services) -> Jinja2Templates:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
        autoescape=jinja2.select_autoescape(["html"]),
        undefined=jinja2.StrictUndefined,  # template typos fail loudly in tests
        trim_blocks=True,
        lstrip_blocks=True,
    )

    def fmt_ts(ts: float | int | None, fmt: str = "%d %b %Y, %H:%M") -> str:
        if ts is None:
            return "—"
        return datetime.fromtimestamp(ts, services.timezone()).strftime(fmt)

    def ago(ts: float | int | None) -> str:
        if ts is None:
            return "never"
        seconds = max(int(time.time() - ts), 0)
        if seconds < 60:
            return f"{seconds}s ago"
        if seconds < 3600:
            return f"{seconds // 60} min ago"
        if seconds < 86400:
            return f"{seconds // 3600} h ago"
        return f"{seconds // 86400} d ago"

    def duration(seconds: float) -> str:
        seconds = int(seconds)
        days, rest = divmod(seconds, 86400)
        hours, rest = divmod(rest, 3600)
        minutes = rest // 60
        if days:
            return f"{days}d {hours}h"
        if hours:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    def body(message) -> str:
        return message_body(message.text, message.media_kind, message.media_meta)

    def num(value: float) -> str:
        return f"{value:g}"

    env.filters.update(ts=fmt_ts, ago=ago, duration=duration, body=body, num=num)

    def context(request: Request) -> dict:
        return {
            "csrf_token": csrf_token(request),
            # Called by base.html, so partial (htmx) responses leave them queued.
            "take_flashes": lambda: request.session.pop("flash", []),
            "nav_path": request.url.path,
            "status": services.status,
            "now": time.time(),
        }

    return Jinja2Templates(env=env, context_processors=[context])
