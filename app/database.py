"""
Async SQLAlchemy engine/session setup.

Kept deliberately generic: swapping DATABASE_URL from a sqlite+aiosqlite
path to a postgresql+asyncpg URL is the only change needed to move to
Postgres. Nothing elsewhere in the app should import a driver-specific
type or construct raw SQL.
"""
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()

# SQLite needs check_same_thread=False for use across the async event loop;
# other drivers ignore connect_args they don't recognize via **kwargs, but to
# stay portable we only pass it when we're actually on SQLite.
_connect_args = {"check_same_thread": False} if _settings.database_url.startswith("sqlite") else {}

engine = create_async_engine(_settings.database_url, connect_args=_connect_args)
SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    """Create tables if they don't exist. Fine for SQLite/dev; use Alembic migrations in production."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields a request-scoped session."""
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Context manager for use outside of a request (e.g. the sync worker)."""
    async with SessionLocal() as session:
        yield session
