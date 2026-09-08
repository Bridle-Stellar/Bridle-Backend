"""
FastAPI application entrypoint.

Wires together the database, the Soroban client, the policy cache, and the
background sync worker at startup, and tears them down cleanly at
shutdown. Run with: uvicorn app.main:app --reload
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.database import init_db
from app.routers import policy, relay, transactions
from app.services.policy_cache import PolicyStateCache
from app.services.soroban_client import SorobanContractClient
from app.services.sync_worker import SyncWorker

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await init_db()

    client = SorobanContractClient(settings)
    app.state.soroban_client = client
    app.state.policy_cache = PolicyStateCache(client, settings.policy_cache_ttl_seconds)

    worker = SyncWorker(client, settings)
    app.state.sync_worker = worker
    if settings.sync_worker_enabled:
        worker.start()

    yield

    if settings.sync_worker_enabled:
        await worker.stop()
    await client.close()


app = FastAPI(
    title="Bridle Backend",
    description=(
        "Off-chain relayer for Bridle: intercepts an AI agent's payment attempts, "
        "checks them against on-chain spending policy (allowlist, caps, kill switch), "
        "and only forwards payments the contract itself has authorized and recorded. "
        "See the README for the confirmed auth model (the agent's own key, not the "
        "relayer's, authorizes every spend) and the two-step /relay/prepare + "
        "/relay/submit flow that follows from it."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(relay.router)
app.include_router(transactions.router)
app.include_router(policy.router)


@app.get("/health", tags=["meta"], summary="Liveness check")
async def health() -> dict:
    return {"status": "ok"}
