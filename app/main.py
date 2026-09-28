"""Application factory.  Run with:  uvicorn app.main:create_app --factory"""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.auth.router import router as auth_router
from app.auth.security import LoginThrottle
from app.auth.users import UserStore
from app.catalog.router import router as catalog_router
from app.catalog.wage_codes import MappingError, WageCodeMapCache
from app.config import Settings, get_settings
from app.core.errors import problem, register_error_handlers
from app.core.logging import audit, configure_logging
from app.core.middleware import BodySizeLimitMiddleware, RequestContextMiddleware
from app.jobs.store import JobStore

log = logging.getLogger("wtc")


async def _sweep_forever(store: JobStore, interval: int) -> None:
    while True:
        try:
            for name in await asyncio.to_thread(store.sweep):
                audit("job_expired", job_id=name)
        except Exception:
            log.exception("Job sweeper failed")
        await asyncio.sleep(interval)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = app.state
        state.settings = settings
        state.user_store = UserStore(settings.user_db_path)
        state.login_throttle = LoginThrottle(
            settings.login_max_failures, settings.login_max_failures_per_ip, settings.login_lockout_minutes * 60)
        state.job_store = JobStore(settings.job_dir, settings.job_ttl_minutes * 60)
        state.wage_codes = WageCodeMapCache(settings.wage_code_map_path)
        state.processing_slots = asyncio.Semaphore(settings.max_concurrent_processing)

        state.wage_codes.get()  # fail fast on a missing or malformed mapping
        state.job_store.check_writable()
        sweeper = asyncio.create_task(_sweep_forever(state.job_store, settings.cleanup_interval_seconds))
        log.info("Wage Type Catalog Service %s ready (jobs: %s, TTL %d min)",
                 __version__, settings.job_dir, settings.job_ttl_minutes)
        try:
            yield
        finally:
            sweeper.cancel()
            with suppress(asyncio.CancelledError):
                await sweeper

    app = FastAPI(
        title="Wage Type Catalog Service",
        version=__version__,
        summary="Payslip Master Data (.xlsx / .csv) → Wage Type Catalog (.xlsx)",
        description=(
            "1. `POST /v1/auth/token` (people) — or send `X-API-Key` (systems).\n"
            "2. `POST /v1/catalog/jobs` with the file.\n"
            "3. `GET` the returned `download_url` once — both files are then deleted.\n\n"
            "Errors are `application/problem+json` with a stable `code` and a `request_id`."
        ),
        lifespan=lifespan,
        docs_url="/docs" if settings.enable_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.enable_docs else None,
    )

    register_error_handlers(app)

    @app.exception_handler(MappingError)
    async def _mapping_error(_: Request, exc: MappingError):
        log.error("Wage-code mapping problem: %s", exc)
        return problem(503, "The wage-code mapping is misconfigured; an administrator has to fix it",
                       code="configuration_error")

    # Added inner-to-outer: RequestContext wraps everything, so even 413s carry a request id.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    if origins := [o.strip() for o in settings.cors_origins.split(",") if o.strip()]:
        app.add_middleware(
            CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Request-ID"],
            expose_headers=["Content-Disposition", "Location", "X-Request-ID", "Retry-After"],
        )
    app.add_middleware(RequestContextMiddleware)

    @app.get("/health/live", tags=["ops"], summary="Process is up")
    def live() -> dict:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["ops"], summary="Dependencies usable (DB, job storage, mapping)")
    def ready(request: Request):
        checks = {}
        for name, probe in (("database", request.app.state.user_store.ping),
                            ("job_storage", request.app.state.job_store.check_writable),
                            ("wage_code_mapping", request.app.state.wage_codes.get)):
            try:
                probe()
                checks[name] = "ok"
            except Exception as e:  # report, don't raise: this endpoint exists to describe failures
                checks[name] = f"failed: {type(e).__name__}"
        if all(v == "ok" for v in checks.values()):
            return {"status": "ok", "checks": checks}
        return problem(503, "One or more dependencies are unavailable", code="not_ready", errors=[checks])

    app.include_router(auth_router)
    app.include_router(catalog_router)
    return app
