"""FastAPI application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .db.session import dispose_engine
from .errors import install_error_handlers
from .routers import auth, health, projects

log = logging.getLogger("hbz")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.assert_production_safe()
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    log.info("starting env=%s db=%s", settings.env,
             settings.database_url.rsplit("/", 1)[-1])
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="hbday-zee",
        summary="Story to video, one supervised stage at a time.",
        version="0.1.0",
        docs_url="/docs" if settings.debug else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,          # the session cookie must survive
        allow_methods=["*"], allow_headers=["*"],
    )
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(auth.me_router)
    app.include_router(projects.router)
    return app


app = create_app()
