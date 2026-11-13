"""
Integration tests for database migration and rollback framework.
"""

import pytest
from src.db.connection import close_pool, get_connection, get_cursor
from src.db.migrations.runner import MigrationRunner


@pytest.fixture(autouse=True)
def setup_and_teardown():
    close_pool()
    yield
    # Rollback everything after test to leave clean slate if needed
    try:
        runner = MigrationRunner()
        runner.rollback(steps=999)
    except Exception:
        pass
    close_pool()


def test_migration_discovery():
    runner = MigrationRunner()
    discovered = runner.discover_migrations()
    assert len(discovered) >= 1
    assert discovered[0].version == 1
    assert discovered[0].name == "initial_schema"
    assert discovered[0].down_path is not None
    assert discovered[0].down_path.exists()


def test_migrate_and_rollback_cycle():
    runner = MigrationRunner()

    # Step 1: Ensure clean starting state
    runner.rollback(steps=999)

    # Step 2: Migrate
    applied = runner.migrate()
    assert 1 in applied

    # Verify tables exist in PostgreSQL
    expected_tables = {
        "ingestion_runs",
        "ingestion_file_checkpoints",
        "dispositions",
        "leads",
        "lead_rejections",
        "interaction_ledger",
        "schema_migrations",
    }
    with get_connection() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public';
                """
            )
            tables = {row[0] for row in cur.fetchall()}
            assert expected_tables.issubset(tables)

    # Verify status reflects applied
    statuses = runner.get_status()
    assert any(s.version == 1 and s.is_applied for s in statuses)

    # Step 3: Re-migrate should be idempotent (no new migrations applied)
    second_applied = runner.migrate()
    assert len(second_applied) == 0

    # Step 4: Rollback
    rolled_back = runner.rollback(steps=1)
    assert 1 in rolled_back

    # Verify tables are dropped
    with get_connection() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name IN ('leads', 'interaction_ledger');
                """
            )
            remaining_tables = cur.fetchall()
            assert len(remaining_tables) == 0

    # Step 5: Re-apply migration to leave schema ready for subsequent tests
    re_applied = runner.migrate()
    assert 1 in re_applied
