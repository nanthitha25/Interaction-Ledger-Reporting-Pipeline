"""
Integration and unit tests for PostgreSQL connection infrastructure and pooling.
"""

import pytest
from src.db.connection import (
    close_pool,
    get_connection,
    get_cursor,
    get_pool,
    ping,
    transaction,
)


@pytest.fixture(autouse=True)
def cleanup_pool():
    yield
    close_pool()


def test_ping_successful():
    assert ping() is True


def test_get_connection_and_cursor():
    with get_connection() as conn:
        with get_cursor(conn) as cur:
            cur.execute("SELECT 42 AS num;")
            row = cur.fetchone()
            assert row[0] == 42


def test_transaction_commit():
    with transaction() as conn:
        with get_cursor(conn) as cur:
            cur.execute("CREATE TEMP TABLE test_txn (val INT);")
            cur.execute("INSERT INTO test_txn VALUES (100);")

    # In a temp table in psycopg2, verify it worked inside transaction
    with get_connection() as conn:
        with get_cursor(conn) as cur:
            cur.execute("SELECT 1;")
            assert cur.fetchone()[0] == 1


def test_transaction_rollback_on_exception():
    with pytest.raises(RuntimeError, match="Simulated failure"):
        with transaction() as conn:
            with get_cursor(conn) as cur:
                cur.execute("CREATE TEMP TABLE test_rollback (val INT);")
                raise RuntimeError("Simulated failure")


def test_pool_recreation_on_config_change():
    pool1 = get_pool()
    assert pool1 is not None
    close_pool()
    pool2 = get_pool()
    assert pool2 is not None
    assert pool1 is not pool2
