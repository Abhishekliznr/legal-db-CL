"""
PostgreSQL connection handling for api-backend.

Standalone by design — no shared package with scraper-backend or the old
api-backend/scraper-backend (docs/scraper-backend-revamp-spec.md §3.2).

Connection is built exclusively from discrete environment variables:
DATABASE_TYPE, DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD.
There is deliberately no DATABASE_URL / DB_CONNECTION key and no
"prefer the URL if set" branching — see spec §3.3.
"""

import os
from contextlib import contextmanager
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import psycopg2
import psycopg2.pool
from psycopg2.extras import RealDictCursor  # noqa: F401 (re-exported for callers)


SUPPORTED_DATABASE_TYPES = {"postgres"}


def _require_env(name: str, default: Optional[str] = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def get_connection_params() -> dict:
    """
    Builds discrete connection parameters from DATABASE_TYPE/DB_HOST/DB_PORT/
    DB_NAME/DB_USER/DB_PASSWORD. No connection-string env var is read.
    """
    db_type = os.environ.get("DATABASE_TYPE", "postgres").strip().lower()
    if db_type not in SUPPORTED_DATABASE_TYPES:
        raise RuntimeError(
            f"Unsupported DATABASE_TYPE: {db_type!r} "
            f"(supported: {sorted(SUPPORTED_DATABASE_TYPES)})"
        )

    return {
        "host": _require_env("DB_HOST", "localhost"),
        "port": int(_require_env("DB_PORT", "5432")),
        "dbname": _require_env("DB_NAME"),
        "user": _require_env("DB_USER"),
        "password": _require_env("DB_PASSWORD"),
    }


def get_connection():
    """
    Fresh, unpooled connection. Used by db/init_db.py's schema-management
    commands (init/ensure-*), which run standalone DDL outside the request
    pool.
    """
    return psycopg2.connect(**get_connection_params())


# ============================================================
# CONNECTION POOL (for the FastAPI request path, e.g. court_config_router)
# ============================================================

_pool: "Optional[psycopg2.pool.ThreadedConnectionPool]" = None

_DEFAULT_POOL_MIN_CONN = 2
_DEFAULT_POOL_MAX_CONN = 10


def init_connection_pool(minconn: Optional[int] = None, maxconn: Optional[int] = None) -> None:
    """
    Creates the process-wide connection pool. Call once, at app startup.

    minconn/maxconn default to DB_POOL_MIN_CONN/DB_POOL_MAX_CONN (2/10) so
    pool size is a per-deployment env var, not a code change. Sizing math
    when scaling horizontally: total connections this service can open
    against Postgres is (replica count) x maxconn — add scraper-backend's
    own (replica count x its maxconn) on top, since both share one
    Postgres instance in production — and keep the sum comfortably under
    Postgres's max_connections (default 100, minus ~3 reserved for
    superuser/replication). Scaling out replica count means *lowering*
    DB_POOL_MAX_CONN to compensate, not leaving it at a per-process
    default and letting the product grow unchecked.
    """
    global _pool
    if _pool is not None:
        return
    if minconn is None:
        minconn = int(os.environ.get("DB_POOL_MIN_CONN", _DEFAULT_POOL_MIN_CONN))
    if maxconn is None:
        maxconn = int(os.environ.get("DB_POOL_MAX_CONN", _DEFAULT_POOL_MAX_CONN))
    _pool = psycopg2.pool.ThreadedConnectionPool(minconn, maxconn, **get_connection_params())


def close_connection_pool() -> None:
    """Closes all pooled connections. Call once, at app shutdown."""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None


@contextmanager
def get_pooled_connection():
    """
    Context manager yielding a connection borrowed from the pool, returning
    it automatically afterward. Rolls back any open transaction before
    returning the connection so a failed request never leaves it dirty for
    the next borrower.

        with connection.get_pooled_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(...)
    """
    if _pool is None:
        raise RuntimeError("Connection pool not initialized — call init_connection_pool() at startup first.")
    conn = _pool.getconn()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)
