"""Local-only HTTP application. Repository roots and credentials are server-owned."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from triageflow.config import Settings
from triageflow.demo import examples
from triageflow.engine import Engine, markdown_report
from triageflow.schemas import CreateRun, Run
from triageflow.store import Store

WEB = Path(__file__).parent / "web"


class BodyLimitMiddleware:
    """Bound actual request bytes, including chunked bodies without Content-Length."""

    def __init__(self, app, limit: int = 32_000):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH", "DELETE"}:
            await self.app(scope, receive, send)
            return
        chunks, total = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > self.limit:
                response = JSONResponse({"detail": "Request body too large."}, status_code=413)
                await response(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        pending = True

        async def replay():
            nonlocal pending
            if pending:
                pending = False
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(settings: Settings | None = None, provider=None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.data_dir / "runs.sqlite3")
    engine = Engine(settings, store, provider)
    tasks: set[asyncio.Task] = set()

    @asynccontextmanager
    async def lifespan(app):
        store.recover()
        yield
        for task in list(tasks):
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(
        title="TriageFlow", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None
    )
    app.state.store, app.state.engine = store, engine
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"]
    )

    @app.middleware("http")
    async def boundaries(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            try:
                expected = urlsplit(str(request.base_url))
                supplied = urlsplit(origin or "")
            except ValueError:
                return JSONResponse({"detail": "Invalid origin."}, status_code=403)
            if origin and (supplied.scheme, supplied.netloc) != (expected.scheme, expected.netloc):
                return JSONResponse(
                    {"detail": "Cross-origin writes are disabled."}, status_code=403
                )
            try:
                too_large = int(request.headers.get("content-length", "0")) > 32_000
            except ValueError:
                return JSONResponse({"detail": "Invalid content length."}, status_code=400)
            if too_large:
                return JSONResponse({"detail": "Request body too large."}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        )
        return response

    def load(run_id: str) -> Run:
        try:
            return store.load(run_id)[0]
        except KeyError:
            raise HTTPException(404, "Run not found") from None

    def schedule(run: Run):
        task = asyncio.create_task(engine.execute(run.id))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def capacity():
        if len(tasks) >= 2:
            raise HTTPException(429, "最多同时执行 2 个任务，请稍后重试。")

    @app.get("/health")
    def health():
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/api/config")
    def config():
        return {
            "mode": settings.mode,
            "model": settings.model if settings.mode == "live" else "deterministic-fixtures",
            "projects": settings.projects(),
            "limits": {"max_steps": settings.max_steps},
        }

    @app.get("/api/examples")
    def get_examples():
        return examples()

    @app.get("/api/runs")
    def runs() -> list[Run]:
        return store.list()

    @app.post("/api/runs", status_code=202)
    async def create(payload: CreateRun) -> Run:
        capacity()
        if len(payload.issue.strip()) < 10:
            raise HTTPException(422, "请提供至少 10 个字符的问题描述。")
        try:
            settings.root(payload.project_id)
        except ValueError:
            raise HTTPException(422, "Unknown project") from None
        run = store.create(payload.project_id, payload.issue.strip(), settings.mode)
        schedule(run)
        return run

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> Run:
        return load(run_id)

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str):
        load(run_id)
        return store.events(run_id)

    @app.post("/api/runs/{run_id}/resume", status_code=202)
    async def resume(run_id: str) -> Run:
        capacity()
        load(run_id)
        try:
            run = store.requeue(run_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        schedule(run)
        return run

    @app.get("/api/runs/{run_id}/report")
    def report(run_id: str):
        run = load(run_id)
        if run.status != "completed" or run.report is None:
            raise HTTPException(409, "Report is not ready")
        return PlainTextResponse(
            markdown_report(run),
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="triageflow-{run.id[:8]}.md"'},
        )

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB / "index.html")

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app
