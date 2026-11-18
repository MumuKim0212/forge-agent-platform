from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from app.access_log import install_access_log_filter
from app.config import Settings, settings
from app.db import SessionLocal, engine
from app.routers import (
    agent_engine,
    approvals,
    audit,
    auth,
    auth_local,
    builds,
    console,
    deployments,
    dev_identity,
    evals,
    notifications as notifications_router,
    preview_auth,
    preview_proxy,
    projects,
    readiness,
    service_proxy,
    templates,
    usage,
)
from app.schema_check import assert_schema_at_head
from app.seed import seed
from app.services.fetch_metadata import FetchMetadataMiddleware
from app.services import notifications
from app.services.local_bootstrap import ensure_local_bootstrap_admin
from app.services.preview_sandbox_cors import SandboxedPreviewCorsMiddleware
from app.services.spa_static import install_spa
from app.services.validation_errors import request_validation_handler


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Alembic is the only thing that creates or alters the platform
    # schema. Refuse to start unless the DB is exactly at the migration head --
    # never create tables here (a server-created DB is one the migration tool
    # can no longer manage). `engine` is read from this module at call time, so
    # a test can redirect it.
    assert_schema_at_head(engine)
    db = SessionLocal()
    try:
        seed(db)
        # ADR 0022 §4: the local-mode bootstrap admin is created at
        # STARTUP (seed), never at first login — nobody can authenticate in
        # `local` mode before this row exists. Applied only when the row is
        # first created (idempotent no-op on every later startup).
        if settings.local_accounts_enabled:
            ensure_local_bootstrap_admin(db)
    finally:
        db.close()
    # a dead worker cannot report itself, so this process watches
    # its lease -- but only when a channel exists to tell anyone (settings are read at startup).
    detector = None
    if notifications.any_channel_configured():
        detector = notifications.WorkerDownDetector(lambda: SessionLocal())
        detector.start()
    try:
        yield
    finally:
        if detector is not None:
            detector.stop()


def create_app(app_settings: Settings | None = None) -> FastAPI:
    """The app factory.

    Exists because one route is registered **conditionally** — the dev
    switcher bootstrap.
    Registration happens once at import time, and the test suite flips auth
    modes by monkeypatching the `settings` singleton, which cannot
    *unregister* an already-registered route. So without a factory the
    section's central security claim ("in `oidc` mode the route does not
    exist at all") would be unverifiable — and an untestable claim invites
    someone to reintroduce a runtime handler check simply because a handler
    check can be tested. Requirement 2 of that section asks for this seam by
    name: a test builds a second app with OIDC settings and asserts the path
    is absent from *that* app's schema.

    `app_settings` defaults to the process-wide singleton, so the module-level
    `app` below is the same application that shipped before."""
    cfg = app_settings or settings

    application = FastAPI(title="Forge Platform API", lifespan=lifespan)

    # no access-log line carries a query-string value (the preview `token`, the OIDC `code` and
    # `state` travel there). Installed here, not in a launcher, so it holds however uvicorn is started.
    install_access_log_filter()

    # a 422 names the field and the failed rule and never echoes what was submitted
    # (FastAPI's default puts pydantic's `input` in every entry -- a rejected password, or a valid
    # one sitting next to the field that failed). One handler, every route, every mode: see
    # `services/validation_errors.py` for why this cannot be a per-field marking.
    application.add_exception_handler(RequestValidationError, request_validation_handler)

    # ADR 0028 layer 3: refuses an unsafe cross-site/same-site write
    # (Fetch Metadata) before it reaches routing. Reads the SAME parsed
    # origin list `CORSMiddleware` gets immediately below — one list,
    # computed once, handed to both — so the two can never drift apart by
    # re-parsing `cfg.cors_origins` twice.
    #
    # Registered FIRST, so `CORSMiddleware` (registered second, below) ends
    # up outside it: Starlette's `add_middleware` inserts each new middleware
    # at the front of the stack, and whichever call happens LAST wraps
    # everything before it. That makes every request reach CORSMiddleware
    # before FetchMetadataMiddleware, so a genuine preflight OPTIONS is
    # answered by CORSMiddleware directly and never reaches
    # FetchMetadataMiddleware at all — see that class's own docstring for
    # the full argument. CORS is outermost only for non-null origins: the preview
    # layer, registered third (below), wraps CORSMiddleware as well.
    cors_origin_list = cfg.cors_origin_list
    #
    # where the preview route is mounted (Traefik off) -- and only there --
    # layer 3 exempts a sandboxed page's `Origin: null` request on the anchored preview shape (the
    # route's token check still runs), and a third layer, added LAST so it is OUTERMOST, answers that
    # shape's CORS for `Origin: null`. It must sit outside `CORSMiddleware`, which would answer a
    # preflight from a non-listed origin with a 400 of its own. In Traefik mode neither is installed:
    # the routes are not mounted and nothing about them changes.
    preview_proxy_mounted = not cfg.traefik_enabled
    application.add_middleware(
        FetchMetadataMiddleware,
        allowed_origins=cors_origin_list,
        exempt_sandboxed_preview=preview_proxy_mounted,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    if preview_proxy_mounted:
        application.add_middleware(SandboxedPreviewCorsMiddleware)

    application.include_router(auth.router, prefix="/api")
    application.include_router(templates.router, prefix="/api")
    application.include_router(projects.router, prefix="/api")
    application.include_router(builds.router, prefix="/api")
    application.include_router(deployments.router, prefix="/api")
    application.include_router(approvals.router, prefix="/api")
    application.include_router(audit.router, prefix="/api")
    application.include_router(preview_auth.router, prefix="/api")
    application.include_router(usage.router, prefix="/api")
    application.include_router(agent_engine.router, prefix="/api")
    application.include_router(evals.router, prefix="/api")
    application.include_router(console.router, prefix="/api")
    application.include_router(notifications_router.router, prefix="/api")

    # ADR 0028 layer 2: the two platform-origin proxy routes exist
    # only where nothing else routes — mounted only when Traefik is not
    # handling routing itself (dev, or prod with Traefik off). In Traefik
    # mode the UI already hands out Traefik URLs for both, and nothing in
    # app/ or the frontend references these paths
    # (ADR 0028 §Decision
    # layer 2). Forward-auth (`preview_auth.router`, above) is unaffected
    # and stays mounted in every mode.
    if preview_proxy_mounted:
        application.include_router(preview_proxy.router, prefix="/api")
        application.include_router(service_proxy.router, prefix="/api")

    # the local-mode signup/login/password/admin-approval surface.
    # Registered only when `local_accounts_enabled` — same conditional-
    # registration idiom as the dev switcher bootstrap below, since every
    # route in `auth_local.router` is meaningless without a password
    # concept.
    if cfg.local_accounts_enabled:
        application.include_router(auth_local.router, prefix="/api")

    # The dev switcher bootstrap. Branches on the derived
    # `session_auth_enabled` **capability** (widened from
    # `oidc_enabled`: `local` mode also resolves identity from the session
    # cookie, so the dev header switcher is equally unreachable/meaningless
    # there), never on `AUTH_MODE` itself — wiring-time code obeys the same
    # no-mode-branching rule as business logic.
    if not cfg.session_auth_enabled:
        application.include_router(dev_identity.router, prefix="/api")

    application.include_router(readiness.router, prefix="/api")

    @application.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # last, and as a 404 fallback, so nothing above is shadowed.
    install_spa(application, cfg.frontend_dist_dir, is_prod=cfg.is_prod)

    return application


app = create_app()
