"""Web admin login, sessions and CSRF protection.

The admin binds to localhost, but it exposes every stored message, so it
still has a password (ADMIN_PASSWORD), a signed session cookie and a CSRF
token on every state-changing request.
"""

import asyncio
import hmac
import logging
import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from naruto.db.database import Database

logger = logging.getLogger(__name__)

SESSION_SECRET_META_KEY = "web_session_secret"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
FAILED_LOGIN_DELAY_SECONDS = 1.0

router = APIRouter()
_login_lock = asyncio.Lock()


class LoginRequired(Exception):
    """Raised by require_admin; turned into a redirect to /login."""


def session_secret(db: Database) -> str:
    """A random secret created on first start and kept in the database, so
    sessions survive restarts without another .env value."""
    secret = db.get_meta(SESSION_SECRET_META_KEY)
    if not secret:
        secret = secrets.token_urlsafe(48)
        db.set_meta(SESSION_SECRET_META_KEY, secret)
    return secret


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def check_csrf(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin != "null" and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(status_code=403, detail="Cross-origin request refused.")
    sent = request.headers.get("x-csrf-token")
    if not sent:
        form = await request.form()
        sent = form.get("csrf_token")
    expected = request.session.get("csrf")
    if not (expected and isinstance(sent, str)
            and hmac.compare_digest(sent.encode(), expected.encode())):
        raise HTTPException(status_code=403,
                            detail="Security token missing or expired. Reload the page and try again.")


async def require_admin(request: Request) -> None:
    """Dependency for every admin route: logged in, and a valid CSRF token
    on anything that changes state."""
    if not request.session.get("admin"):
        raise LoginRequired()
    if request.method not in SAFE_METHODS:
        await check_csrf(request)


def login_redirect(request: Request, exc: LoginRequired) -> Response:
    if request.headers.get("hx-request"):
        return Response(status_code=204, headers={"HX-Redirect": "/login"})
    target = request.url.path
    if request.url.query:
        target += f"?{request.url.query}"
    return RedirectResponse(f"/login?next={target}" if target != "/" else "/login",
                            status_code=303)


def _safe_next(value: str | None) -> str:
    """Only allow local paths, never another host."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


@router.get("/login")
async def login_page(request: Request, next: str | None = None):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "login.html",
                                      {"error": None, "next": _safe_next(next)})


@router.post("/login")
async def login(request: Request):
    await check_csrf(request)
    form = await request.form()
    password = form.get("password")
    next_path = _safe_next(form.get("next"))
    expected = request.app.state.services.bootstrap.admin_password
    async with _login_lock:
        ok = (isinstance(password, str) and bool(expected)
              and hmac.compare_digest(password.encode(), expected.encode()))
        if not ok:
            # Serialized and slowed down, so guessing is limited to about one
            # attempt per second.
            await asyncio.sleep(FAILED_LOGIN_DELAY_SECONDS)
    if not ok:
        logger.warning("Failed web admin login")
        templates = request.app.state.templates
        return templates.TemplateResponse(request, "login.html",
                                          {"error": "Wrong password.", "next": next_path},
                                          status_code=401)
    request.session.clear()  # new session on login
    request.session["admin"] = True
    csrf_token(request)
    logger.info("Web admin login")
    return RedirectResponse(next_path, status_code=303)


@router.post("/logout")
async def logout(request: Request):
    if request.session.get("admin"):
        await check_csrf(request)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
