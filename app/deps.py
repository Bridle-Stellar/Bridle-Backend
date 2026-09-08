"""
FastAPI dependency providers. Routers depend on these instead of reaching
into app.state directly, so routers stay easy to unit test with overrides.
"""
from fastapi import Request

from app.config import Settings, get_settings
from app.services.policy_cache import PolicyStateCache
from app.services.soroban_client import SorobanContractClient


def get_app_settings() -> Settings:
    return get_settings()


def get_soroban_client(request: Request) -> SorobanContractClient:
    return request.app.state.soroban_client


def get_policy_cache(request: Request) -> PolicyStateCache:
    return request.app.state.policy_cache
