"""FastAPI application: WebSocket hub, health, metrics, read-only API, SPA.

**The HTTP surface is read-only by construction.** The dashboard is exposed to
the internet without authentication, so there is no POST/PUT/PATCH/DELETE route
anywhere in this package and none may be added: every write path in the system
is driven by UDP packets from the game, never by an HTTP caller. `tests/
test_web.py::test_no_mutating_routes` enforces this by walking the route table.

Everything the app needs is injected — the live state, the components whose
counters feed `/metrics`, and how to reach the database — so the app can be
built and tested without a game, a socket, or Postgres.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import uvicorn
from fastapi import APIRouter, FastAPI, HTTPException, Query, WebSocket
from fastapi import Path as PathParam
from fastapi.responses import HTMLResponse, PlainTextResponse
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from f126.web import metrics
from f126.web.broadcast import WsHub

if TYPE_CHECKING:
    from f126.config import Config
    from f126.web.interfaces import LiveSource, StatsSources

log = logging.getLogger(__name__)

#: Same-origin everything. `script-src 'self'` (no 'unsafe-inline') is why the
#: FastAPI doc UIs are disabled below — they load their bundles off a CDN and
#: would render blank. Inline styles are allowed because bundlers inject them.
CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self' ws: wss:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)

#: Shown at `/` when the frontend has not been built. Deliberately dependency
#: free and inline-styled: it has to work when `frontend/dist` does not exist.
FALLBACK_HTML = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>f126 — frontend not built</title>
<style>
  :root { color-scheme: dark }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center;
         background: #0b0d10; color: #e6e8eb;
         font: 15px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace }
  main { max-width: 34rem; padding: 2rem }
  h1 { font-size: 1.1rem; letter-spacing: .12em; text-transform: uppercase; color: #8b93a1 }
  code { background: #171a1f; padding: .15rem .4rem; border-radius: 4px; color: #7ee787 }
  a { color: #58a6ff }
</style>
<main>
  <h1>f126 race engineer</h1>
  <p>frontend not built — run <code>make frontend</code></p>
  <p>The backend is up: <a href="/healthz">/healthz</a>, <a href="/metrics">/metrics</a>,
     <a href="/api/sessions">/api/sessions</a>, and the live socket at <code>/ws</code>
     are all serving.</p>
</main>
"""

#: Helpers in `store.queries`, most-preferred name first. The real names lead;
#: the alternates are tolerance for a rename on the store side, since a naming
#: near-miss should degrade one endpoint, not break the build.
_QUERY_NAMES: dict[str, tuple[str, ...]] = {
    "sessions": ("list_sessions", "recent_sessions", "sessions"),
    "session": ("session_detail", "get_session", "session"),
    "laps": ("laps_for_session", "list_laps", "session_laps", "laps"),
}

_DB_UNAVAILABLE = "database unavailable"

#: A recorded session is addressed by the integer `sessions.id` the store keys
#: on (wide enough to also carry a u64 uid). Anything else is rejected with a
#: 422 before it can reach a query — this endpoint is unauthenticated, so junk
#: stops at the door. Module level because postponed annotations mean FastAPI
#: resolves this name in the module scope.
SessionId = Annotated[str, PathParam(pattern=r"^[0-9]{1,20}$")]


class SecurityHeadersMiddleware:
    """Stamp the security headers on every HTTP response.

    Raw ASGI rather than `BaseHTTPMiddleware`: this only needs to touch one
    message type, and it stays out of the way of streaming file responses and
    the WebSocket scope entirely.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("x-content-type-options", "nosniff")
                headers.setdefault("content-security-policy", CSP)
                headers.setdefault("referrer-policy", "no-referrer")
                headers.setdefault("x-frame-options", "DENY")
            await send(message)

        await self.app(scope, receive, send_with_headers)


class SpaStaticFiles(StaticFiles):
    """Static files with an SPA history fallback.

    An unknown path is a client-side route, not a missing file, so it resolves
    to `index.html` and the frontend router takes it from there. Requests under
    `/api` and `/ws` never reach here — those routes are registered first.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or path == "index.html":
                raise
            try:
                return await super().get_response("index.html", scope)
            except StarletteHTTPException:
                # A static dir with no index.html: the build is half-there.
                if path in ("", "."):
                    return HTMLResponse(FALLBACK_HTML)
                raise exc from None


def create_app(
    cfg: Config,
    live: LiveSource,
    stats_sources: StatsSources | None = None,
    db_conn_factory: Callable[[], Any] | None = None,
) -> FastAPI:
    """Build the ASGI app.

    Args:
        cfg: process config; `ws_*` and `static_dir` are read here.
        live: the state layer's read side, fanned out over `/ws`.
        stats_sources: named components whose `stats()` dicts become
            `f126_{name}_*` metrics. Every entry is optional and a None value is
            skipped, so a run with the DB writer disabled just has fewer series.
            The name "ws" is reserved for the hub's own counters.
        db_conn_factory: called per request to get a DB connection for the
            read-only API; None (no `F126_DATABASE_URL`) means those endpoints
            answer 503. If the returned object is a context manager — a bare
            psycopg connection or `pool.connection()` — it is entered and exited
            around the query, so connections are not leaked.
    """
    sources = dict(stats_sources or {})
    hub = WsHub(cfg, live)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await hub.start()
        try:
            yield
        finally:
            await hub.aclose()

    app = FastAPI(
        title="f126 race engineer",
        version="0.1.0",
        summary="Live F1 telemetry dashboard — read-only HTTP surface.",
        lifespan=lifespan,
        docs_url=None,  # Swagger/ReDoc load from a CDN; our CSP blocks that.
        redoc_url=None,
        openapi_url=None,  # internet-exposed surface: don't publish the route table
    )
    app.add_middleware(SecurityHeadersMiddleware)
    # The state layer mirrors client_count into `slow.health.ws_clients`, and the
    # orchestrator may want the hub for shutdown ordering.
    app.state.ws_hub = hub

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        """Liveness only: 200 whenever the process can serve a request.

        Deliberately independent of telemetry and the database — a race weekend
        with the game off is not an unhealthy process, and a restart would not
        fix a missing DB. Readiness signals live in `/metrics`.
        """
        return {"status": "ok"}

    @app.get("/metrics", tags=["ops"], response_class=PlainTextResponse)
    async def metrics_endpoint() -> PlainTextResponse:
        """Prometheus exposition for every component that reported in."""
        collected: dict[str, Any] = {}
        for name, source in sources.items():
            if source is None:
                continue
            try:
                collected[name] = source.stats()
            except Exception:
                log.exception("stats source %r raised; omitting its metrics", name)
        collected["ws"] = hub.stats()
        return PlainTextResponse(metrics.render(collected), media_type=metrics.CONTENT_TYPE)

    app.include_router(_api_router(db_conn_factory))
    from f126.web.analysis_routes import analysis_router  # noqa: PLC0415 — Phase 2 analysis API

    app.include_router(analysis_router(db_conn_factory))

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await hub.handle(websocket)

    _mount_frontend(app, cfg)
    return app


# ---- read-only REST API ------------------------------------------------


def _api_router(db_conn_factory: Callable[[], Any] | None) -> APIRouter:
    """The `/api` surface: three GETs over recorded sessions, nothing else."""
    router = APIRouter(prefix="/api", tags=["read-only"])

    @router.get("/sessions", response_model=None)
    async def list_sessions(
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> Any:
        """Most recent sessions, newest first."""
        return await _query(db_conn_factory, "sessions", limit)

    @router.get("/sessions/{session_id}", response_model=None)
    async def get_session(session_id: SessionId) -> Any:
        """One session's detail row: counts, participants, tyre stints."""
        row = await _query(db_conn_factory, "session", int(session_id))
        if row is None:
            raise HTTPException(status_code=404, detail="session not found")
        return row

    @router.get("/sessions/{session_id}/laps", response_model=None)
    async def list_laps(
        session_id: SessionId,
        car_index: Annotated[int | None, Query(ge=0, le=21)] = None,
    ) -> Any:
        """Every recorded lap of a session, optionally for one car only.

        The filter argument is only passed when asked for, so the helper is
        called with the same two positional args it has always taken.
        """
        args: tuple[Any, ...] = (int(session_id),)
        if car_index is not None:
            args += (car_index,)
        return await _query(db_conn_factory, "laps", *args)

    return router


async def _query(factory: Callable[[], Any] | None, helper: str, *args: Any) -> Any:
    """Run one `store.queries` helper off the event loop.

    Every failure mode collapses to 503 `database unavailable`: no DB
    configured, the store module not importable, the helper missing, or the
    query itself blowing up. The detail is deliberately opaque — this endpoint
    is unauthenticated — and the real reason goes to the log.
    """
    if factory is None:
        raise HTTPException(status_code=503, detail=_DB_UNAVAILABLE)

    try:
        from f126.store import queries  # noqa: PLC0415 — built by another workstream
    except ImportError as exc:
        log.warning("store.queries not importable (%s); /api answering 503", exc)
        raise HTTPException(status_code=503, detail=_DB_UNAVAILABLE) from None

    candidates = _QUERY_NAMES[helper]
    fn = next((getattr(queries, name) for name in candidates if hasattr(queries, name)), None)
    if fn is None:
        log.warning("store.queries exposes none of %s; /api answering 503", candidates)
        raise HTTPException(status_code=503, detail=_DB_UNAVAILABLE)

    try:
        return await asyncio.to_thread(_call_with_connection, factory, fn, *args)
    except Exception:
        log.exception("query %s failed", helper)
        raise HTTPException(status_code=503, detail=_DB_UNAVAILABLE) from None


def _call_with_connection(factory: Callable[[], Any], fn: Callable[..., Any], *args: Any) -> Any:
    """Acquire a connection, run `fn(conn, *args)`, release it. Runs in a thread.

    psycopg connections and pool handles are both context managers, and entering
    them is what commits/returns/closes; a factory that hands back something
    else is used as-is and left alone.
    """
    handle = factory()
    if hasattr(handle, "__enter__"):
        with handle as conn:
            return fn(conn if conn is not None else handle, *args)
    return fn(handle, *args)


# ---- static frontend ----------------------------------------------------


def _mount_frontend(app: FastAPI, cfg: Config) -> None:
    """Serve the built SPA at `/`, or explain how to build it.

    Mounted last: `/healthz`, `/metrics`, `/api/*` and `/ws` are matched in
    registration order, so a mount at `/` cannot shadow them.
    """
    directory = Path(cfg.static_dir).expanduser()
    if directory.is_dir():
        app.mount("/", SpaStaticFiles(directory=directory, html=True), name="static")
        return

    log.warning("static dir %s does not exist; serving the placeholder page", directory)

    @app.get("/", include_in_schema=False, response_class=HTMLResponse)
    async def frontend_missing() -> HTMLResponse:
        return HTMLResponse(FALLBACK_HTML)


# ---- serving ------------------------------------------------------------


class _Server(uvicorn.Server):
    """uvicorn server that keeps its hands off the process' signal handlers.

    The orchestrator installs its own — it has a UDP listener, a DB writer and a
    raw log to shut down in order, and uvicorn stealing SIGINT would cut that
    short.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Any:
        yield


def build_server(cfg: Config, app: ASGIApp) -> uvicorn.Server:
    """A configured `uvicorn.Server`; the caller owns its lifecycle.

    Use this when you need the handle (set `server.should_exit = True` for a
    graceful stop); use `run()` when you just want the coroutine.
    """
    config = uvicorn.Config(
        app,
        host=cfg.http_host,
        port=cfg.http_port,
        log_level="info",
        access_log=False,  # a dashboard polling at 10 Hz makes access logs noise
        server_header=False,
        lifespan="on",
        ws_ping_interval=20.0,  # prunes half-open sockets so the cap stays honest
        ws_ping_timeout=20.0,
        ws_max_size=16 * 1024,  # server->client protocol; inbound frames are noise
        limit_concurrency=64,  # internet-exposed: bound sockets, not just registered clients
        timeout_graceful_shutdown=5,
    )
    return _Server(config)


def run(cfg: Config, app: ASGIApp) -> Coroutine[Any, Any, None]:
    """Return the `serve()` coroutine for the orchestrator's task group.

    Not awaited and not `uvicorn.run()`: the web server is one task among
    several in a single-process service, and the process' event loop belongs to
    the orchestrator.
    """
    return build_server(cfg, app).serve()
