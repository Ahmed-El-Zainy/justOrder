"""MongoDB access for the serving path.

Uses PyMongo's native `AsyncMongoClient`. Motor is deliberately absent: it
reached end of life on 2026-05-14 (research.md R1).

The client here connects with the read-only application credential. Nothing in
the serving path is permitted a write-capable connection — that is one half of
constitution Principle II, the other being the stage allow-list in
`agent/guards.py`.
"""

from __future__ import annotations

from typing import Any

import structlog
from pymongo import AsyncMongoClient
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase

from app.config import get_settings

log = structlog.get_logger(__name__)

_client: AsyncMongoClient[dict[str, Any]] | None = None


async def connect() -> AsyncMongoClient[dict[str, Any]]:
    """Open the connection pool. Called from the FastAPI lifespan."""
    global _client
    if _client is None:
        settings = get_settings()
        _client = AsyncMongoClient(
            settings.mongo_uri,
            serverSelectionTimeoutMS=5_000,
            # Keeps a slow aggregation from occupying a connection indefinitely.
            timeoutMS=settings.query_timeout_ms + 5_000,
        )
        await _client.aconnect()
        log.info("mongo.connected", database=settings.mongo_db)
    return _client


async def disconnect() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None
        log.info("mongo.disconnected")


def get_client() -> AsyncMongoClient[dict[str, Any]]:
    if _client is None:
        raise RuntimeError("MongoDB client not connected — call connect() first")
    return _client


def get_database() -> AsyncDatabase[dict[str, Any]]:
    return get_client()[get_settings().mongo_db]


def get_collection() -> AsyncCollection[dict[str, Any]]:
    """The one collection the agent is permitted to query."""
    return get_database()[get_settings().mongo_collection]


def get_vocabulary_collection() -> AsyncCollection[dict[str, Any]]:
    return get_database()[get_settings().vocabulary_collection]


async def ping() -> bool:
    try:
        await get_client().admin.command("ping")
        return True
    except Exception as exc:
        log.warning("mongo.ping_failed", error=str(exc))
        return False


async def document_count() -> int:
    """Row count for the health endpoint.

    Distinguishes "the stack is up but nobody loaded the data" from a real
    fault, which would otherwise present as a confusing run of empty answers.
    """
    try:
        return await get_collection().estimated_document_count()
    except Exception as exc:
        log.warning("mongo.count_failed", error=str(exc))
        return 0
