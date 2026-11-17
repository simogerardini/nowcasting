from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import get_settings

settings = get_settings()

# Small pool: Supabase free pooler allows few connections (api + worker share them)
engine = create_async_engine(settings.database_url, pool_size=settings.db_pool_size,
                             max_overflow=settings.db_max_overflow, pool_pre_ping=True, pool_recycle=1800)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


async def init_db() -> None:
    """Apply the idempotent schema (safe on every start)."""
    lines = [ln for ln in SCHEMA_PATH.read_text().splitlines() if not ln.strip().startswith("--")]
    statements = [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
    async with engine.begin() as conn:
        # Serialize concurrent initialisers (api + worker starting together)
        await conn.execute(text("SELECT pg_advisory_xact_lock(424242)"))
        for stmt in statements:
            await conn.execute(text(stmt))


async def get_session():
    async with SessionLocal() as session:
        yield session
