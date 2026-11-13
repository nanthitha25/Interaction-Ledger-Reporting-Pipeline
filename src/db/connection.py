"""
PostgreSQL Connection Infrastructure and Pooling.

Provides thread-safe connection pooling, transaction context management,
and health check operations.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Generator, Optional

import psycopg2
from psycopg2.extensions import connection as PgConnection
from psycopg2.extensions import cursor as PgCursor
from psycopg2.pool import ThreadedConnectionPool

from src.config import DatabaseConfig, load_config

logger = logging.getLogger(__name__)

_pool: Optional[ThreadedConnectionPool] = None
_pool_lock = threading.Lock()
_current_pool_url: Optional[str] = None


class DatabaseError(Exception):
    """Base exception for database infrastructure errors."""


class ConnectionPoolError(DatabaseError):
    """Raised when connection pool operations fail."""


def get_pool(config: Optional[DatabaseConfig] = None) -> ThreadedConnectionPool:
    """
    Returns the thread-safe singleton connection pool, initializing it if necessary.
    If the requested database URL differs from the active pool, the existing pool is closed and re-created.
    """
    global _pool, _current_pool_url
    if config is None:
        app_config = load_config()
        config = app_config.db

    with _pool_lock:
        if _pool is not None and _current_pool_url != config.url:
            close_pool()

        if _pool is None:
            try:
                _pool = ThreadedConnectionPool(
                    minconn=config.min_connections,
                    maxconn=config.max_connections,
                    dsn=config.url,
                )
                _current_pool_url = config.url
                logger.info("Initialized PostgreSQL ThreadedConnectionPool (url=%s)", config.url)
            except Exception as e:
                raise ConnectionPoolError(f"Failed to initialize PostgreSQL connection pool: {e}") from e

        return _pool


def close_pool() -> None:
    """Closes all connections in the pool and resets pool state."""
    global _pool, _current_pool_url
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.closeall()
                logger.info("Closed PostgreSQL ThreadedConnectionPool.")
            except Exception as e:
                logger.warning("Error closing connection pool: %s", e)
            finally:
                _pool = None
                _current_pool_url = None


@contextmanager
def get_connection(config: Optional[DatabaseConfig] = None) -> Generator[PgConnection, None, None]:
    """
    Context manager that checks out a connection from the pool and guarantees its return.
    """
    pool = get_pool(config)
    conn = pool.getconn()
    try:
        yield conn
    finally:
        pool.putconn(conn)


@contextmanager
def get_cursor(conn: PgConnection) -> Generator[PgCursor, None, None]:
    """
    Context manager that creates a database cursor and ensures it is closed upon exit.
    """
    cur = conn.cursor()
    try:
        yield cur
    finally:
        cur.close()


@contextmanager
def transaction(
    conn: Optional[PgConnection] = None,
    config: Optional[DatabaseConfig] = None,
) -> Generator[PgConnection, None, None]:
    """
    Context manager for atomic database transactions.
    If an active connection is passed, it uses it; otherwise, checks out a connection.
    Automatically commits on normal completion or rolls back on exception.
    """
    if conn is not None:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    else:
        with get_connection(config) as new_conn:
            try:
                yield new_conn
                new_conn.commit()
            except Exception:
                new_conn.rollback()
                raise


def ping(config: Optional[DatabaseConfig] = None) -> bool:
    """
    Verifies that the database is reachable and accepting queries.
    """
    try:
        with get_connection(config) as conn:
            with get_cursor(conn) as cur:
                cur.execute("SELECT 1;")
                row = cur.fetchone()
                return row is not None and row[0] == 1
    except Exception as e:
        logger.debug("Database ping failed: %s", e)
        return False
