"""FastAPI application for the web admin (served in the bot's process)."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from naruto.services import Services
from naruto.web import (
    auth,
    board,
    chat_settings,
    chats,
    history,
    imports,
    logs_page,
    memory,
    pages,
    people,
    queue,
    runs,
    settings_pages,
)
from naruto.web.templating import make_templates

STATIC_DIR = Path(__file__).parent / "static"
SESSION_MAX_AGE = 7 * 24 * 3600

SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
    ),
}


def create_app(services: Services, *, session_secret: str) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.services = services
    app.state.templates = make_templates(services)
    app.state.chat_detail_extras = [imports.chat_imports, board.chat_board, memory.chat_memory,
                                     history.chat_history]

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            if name == "Cache-Control" and request.url.path.startswith("/static/"):
                continue
            response.headers.setdefault(name, value)
        return response

    # Added last, so it runs first and the session exists for everything else.
    app.add_middleware(SessionMiddleware, secret_key=session_secret,
                       session_cookie="naruto_admin", max_age=SESSION_MAX_AGE,
                       same_site="strict")

    app.add_exception_handler(auth.LoginRequired, auth.login_redirect)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.headers.get("hx-request") or not request.session.get("admin"):
            return PlainTextResponse(str(exc.detail), status_code=exc.status_code)
        return app.state.templates.TemplateResponse(
            request, "error.html", {"status_code": exc.status_code, "detail": exc.detail},
            status_code=exc.status_code)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return PlainTextResponse("ok")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(auth.router)
    app.include_router(pages.router)
    app.include_router(chats.router)
    app.include_router(chat_settings.router)
    app.include_router(board.router)
    app.include_router(memory.router)
    app.include_router(history.router)
    app.include_router(people.router)
    app.include_router(imports.router)
    app.include_router(runs.router)
    app.include_router(queue.router)
    app.include_router(settings_pages.router)
    app.include_router(logs_page.router)
    return app
