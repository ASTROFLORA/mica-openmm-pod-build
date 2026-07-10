# Database connection management for NEON PostgreSQL
import asyncpg
import asyncio
import os
import ssl
from urllib.parse import urlparse, parse_qs
from typing import Optional
import logging

from mica.config.dotenv_loader import normalize_env_value
from mica.infrastructure.persistence.pg_async import asyncpg_connection_kwargs_for_database_url
from mica.infrastructure.unified_backend.config import settings
try:
    from src.models.mudo import (
        MUDO_TABLE_SQL,
        MUDO_VERSION_TABLE_SQL,
        MUDO_INDEXES_SQL,
        MUDO_VERSION_INDEX_SQL,
        MUDO_COLLABORATORS_TABLE_SQL,
        MUDO_COLLABORATORS_INDEX_SQL,
    )
    from src.services.workspace_manager import (
        WORKSPACE_TABLE_SQL,
        WORKSPACE_INSTANCE_TABLE_SQL,
        MUDO_TRANSFER_TABLE_SQL,
    )
except ModuleNotFoundError:
    from models.mudo import (
        MUDO_TABLE_SQL,
        MUDO_VERSION_TABLE_SQL,
        MUDO_INDEXES_SQL,
        MUDO_VERSION_INDEX_SQL,
        MUDO_COLLABORATORS_TABLE_SQL,
        MUDO_COLLABORATORS_INDEX_SQL,
    )
    from services.workspace_manager import (
        WORKSPACE_TABLE_SQL,
        WORKSPACE_INSTANCE_TABLE_SQL,
        MUDO_TRANSFER_TABLE_SQL,
    )

logger = logging.getLogger(__name__)

# Connection pool
_connection_pool: Optional[asyncpg.Pool] = None


def _environment_mode() -> str:
    return (
        normalize_env_value(os.getenv("MICA_ENV"))
        or normalize_env_value(os.getenv("ENVIRONMENT"))
        or normalize_env_value(os.getenv("APP_ENV"))
        or ""
    ).lower()


def _is_production_mode() -> bool:
    return _environment_mode() in {"prod", "production"}


def _database_startup_failure_allowed() -> bool:
    return (os.getenv("ALLOW_DB_STARTUP_FAILURE") or "").strip() == "1"


def _normalize_database_url_candidate(value: str | None) -> str | None:
    normalized = normalize_env_value(value)
    if not normalized:
        return None

    parsed = urlparse(normalized)
    if parsed.scheme not in {"postgres", "postgresql"}:
        logger.warning(
            "Ignoring invalid database URL candidate with unsupported scheme: %r",
            parsed.scheme,
        )
        return None
    return normalized


def _current_database_url() -> str | None:
    # Allow explicit opt-out for offline demos/tests.
    if (os.getenv("MICA_DISABLE_DATABASE") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return None

    # If the process explicitly sets an empty value, treat that as "disabled"
    # (and do NOT fall back to values loaded earlier from .env files).
    if "DATABASE_URL" in os.environ and normalize_env_value(os.environ.get("DATABASE_URL")) is None:
        return None
    if "NEON_DATABASE_URL" in os.environ and normalize_env_value(os.environ.get("NEON_DATABASE_URL")) is None:
        return None

    # Read from the live process env first (env-file may be loaded after module import).
    for candidate in (
        os.getenv("NEON_DATABASE_URL"),
        os.getenv("DATABASE_URL"),
        settings.DATABASE_URL,
    ):
        normalized = _normalize_database_url_candidate(candidate)
        if normalized:
            return normalized
    return None


def _ssl_context_for_database_url(database_url: str) -> ssl.SSLContext | None:
    try:
        parsed = urlparse(database_url)
        sslmode = (parse_qs(parsed.query).get("sslmode", [""])[0] or "").strip().lower()
    except Exception:
        sslmode = ""

    if not sslmode:
        sslmode = (os.getenv("PGSSLMODE") or "").strip().lower()

    # Tiger/Timescale cloud often presents a certificate chain that fails Windows
    # verification (e.g. "self-signed certificate in chain"). For `sslmode=require`
    # the intent is encryption-in-transit without strict verification.
    if sslmode == "require":
        ctx = ssl.create_default_context()
        _skip_verify = os.getenv("MICA_SSL_VERIFY_SKIP", "").strip().lower() in ("1", "true", "yes")
        if _skip_verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            logger.warning(
                "SSL verification disabled for database connection "
                "(MICA_SSL_VERIFY_SKIP=1, sslmode=require)."
            )
        return ctx

    if sslmode in {"verify-ca", "verify-full"}:
        return ssl.create_default_context()
    return None


async def create_connection_pool():
    """Create connection pool to NEON database"""
    global _connection_pool

    database_url = _current_database_url()
    if not database_url:
        logger.warning("DATABASE_URL not configured")
        return None

    try:
        _connection_pool = await asyncpg.create_pool(
            min_size=1,
            max_size=3,  # Limited for 2GB instance
            timeout=20,
            command_timeout=60,
            server_settings={
                "jit": "off"  # Disable JIT for memory efficiency
            },
            **asyncpg_connection_kwargs_for_database_url(database_url),
        )
        logger.info("✅ Database connection pool created")
        return _connection_pool
    except asyncio.TimeoutError:
        logger.warning(
            "Database connection pool creation timed out after 20s; continuing without DB pool in this process."
        )
        return None
    except Exception as e:
        logger.warning(
            "Database connection pool creation failed (%s): %s",
            type(e).__name__,
            e,
        )
        return None


async def get_database_connection():
    """Get database connection from pool"""
    global _connection_pool

    if not _connection_pool:
        _connection_pool = await create_connection_pool()

    if _connection_pool:
        try:
            return await _connection_pool.acquire()
        except Exception as e:
            logger.error(f"Failed to acquire database connection: {e}")
            return None

    return None


async def release_database_connection(connection):
    """Release database connection back to pool"""
    global _connection_pool

    if _connection_pool and connection:
        try:
            await _connection_pool.release(connection)
        except Exception as e:
            logger.error(f"Failed to release database connection: {e}")


async def close_database_pool():
    """Close database connection pool"""
    global _connection_pool

    if _connection_pool:
        await _connection_pool.close()
        _connection_pool = None
        logger.info("Database connection pool closed")


# Database dependency for FastAPI
async def get_db():
    """FastAPI dependency for database connections"""
    connection = await get_database_connection()
    try:
        yield connection
    finally:
        if connection:
            await release_database_connection(connection)


# Initialize common tables for Astroflora
async def initialize_database():
    """Initialize database tables if they don't exist"""
    database_url = _current_database_url()
    if not database_url:
        logger.info("No database URL configured, skipping initialization")
        return

    connection = await get_database_connection()
    if not connection:
        if _is_production_mode() and not _database_startup_failure_allowed():
            raise RuntimeError(
                "Database startup check failed in production: database is configured but unreachable. "
                "Set ALLOW_DB_STARTUP_FAILURE=1 only for non-production emergency diagnostics."
            )
        logger.warning("Skipping database initialization because no connection is available")
        return

    try:
        async def _exec(label: str, sql: str) -> None:
            try:
                await connection.execute(sql)
            except Exception as exc:
                logger.error(f"❌ DB init failed at {label}: {exc}")

        # Create protein_analyses table
        await _exec(
            "protein_analyses",
            """
            CREATE TABLE IF NOT EXISTS protein_analyses (
                id SERIAL PRIMARY KEY,
                protein_id VARCHAR(255) NOT NULL,
                organism VARCHAR(255),
                analysis_type VARCHAR(100),
                results JSONB,
                confidence_level VARCHAR(50),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )

        # Create experiments table
        await _exec(
            "experiments",
            """
            CREATE TABLE IF NOT EXISTS experiments (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                description TEXT,
                experiment_data JSONB,
                status VARCHAR(50) DEFAULT 'draft',
                created_by VARCHAR(255),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )

        # Create mcp_sessions table
        await _exec(
            "mcp_sessions",
            """
            CREATE TABLE IF NOT EXISTS mcp_sessions (
                id SERIAL PRIMARY KEY,
                session_id VARCHAR(255) UNIQUE,
                messages JSONB DEFAULT '[]',
                context JSONB DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )

        # Create M-UDO tables
        await _exec("mudos_table", MUDO_TABLE_SQL)

        # Repair legacy schemas: older deployments may have `mudos` without `entity_type`.
        # This avoids failing later index creation with: column "entity_type" does not exist
        await _exec(
            "mudos_entity_type_column",
            """
            ALTER TABLE mudos
            ADD COLUMN IF NOT EXISTS entity_type VARCHAR(100) NOT NULL DEFAULT 'unknown';
            """,
        )

        await _exec("mudo_versions_table", MUDO_VERSION_TABLE_SQL)
        await _exec("mudos_indexes", MUDO_INDEXES_SQL)
        await _exec("mudo_versions_indexes", MUDO_VERSION_INDEX_SQL)

        # Create M-UDO collaborators/ACL tables
        await _exec("mudo_collaborators_table", MUDO_COLLABORATORS_TABLE_SQL)
        await _exec("mudo_collaborators_indexes", MUDO_COLLABORATORS_INDEX_SQL)

        # Create Workspace tables
        await _exec("workspaces_table", WORKSPACE_TABLE_SQL)
        await _exec("workspace_instances_table", WORKSPACE_INSTANCE_TABLE_SQL)
        await _exec("mudo_transfer_table", MUDO_TRANSFER_TABLE_SQL)

        logger.info("✅ Database tables initialized successfully")

    except Exception as e:
        logger.error(f"❌ Failed to initialize database: {e}")
    finally:
        if connection:
            await release_database_connection(connection)
