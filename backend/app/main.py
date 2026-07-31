"""FastAPI application entry point."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.agent import llm
from app.config import get_settings
from app.db import client as db
from app.models.schemas import Health, LLMHealth, MongoHealth


def configure_logging(level: str) -> None:
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper(), logging.INFO))
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    await db.connect()

    # Warm the grounding vocabulary so the first question is not slower than
    # the rest. A failure here is not fatal — the agent falls back to querying.
    try:
        from app.agent import vocabulary

        await vocabulary.load()
    except Exception as exc:
        log.warning("vocabulary.warmup_failed", error=str(exc))

    log.info("app.started", model=settings.llm_model)
    yield
    await db.disconnect()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Procurement Chat Assistant API",
        version="1.0.0",
        description=(
            "Conversational access to the State of California large-purchases dataset. "
            "Questions become validated, read-only MongoDB aggregation pipelines."
        ),
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from app.api.routes import router

    app.include_router(router)

    @app.get("/health", response_model=Health, tags=["health"])
    async def health() -> JSONResponse:
        connected = await db.ping()
        count = await db.document_count() if connected else 0
        llm_ok = await llm.reachable()

        # An empty collection is "degraded", not "ok". Otherwise "the stack is up
        # but nobody loaded the data" presents as a confusing run of empty answers.
        healthy = connected and count > 0 and llm_ok

        payload = Health(
            status="ok" if healthy else "degraded",
            mongo=MongoHealth(connected=connected, document_count=count),
            llm=LLMHealth(
                provider="openrouter",
                model=settings.llm_model,
                reachable=llm_ok,
            ),
        )
        return JSONResponse(
            status_code=200 if healthy else 503,
            content=payload.model_dump(mode="json"),
        )

    return app


app = create_app()
