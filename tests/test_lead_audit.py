"""
Audit Verification Test Suite for LEAD-08.

Verifies:
1. Transaction and Checkpoint Atomicity (Failure Injection at all 5 lifecycle points).
2. All Six Idempotency Scenarios from specs/05-database-design.md.
"""

import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
import pytest

from src.db.connection import close_pool, get_connection, get_cursor, transaction
from src.db.migrations.runner import MigrationRunner
from src.ingestion.lead_pipeline import (
    FileIntegrityViolationError,
    LeadPipelineError,
    ingest_lead_csv,
)
from src.repository.lead_repository import LeadRepository


@pytest.fixture(scope="module", autouse=True)
def apply_migrations():
    """Ensures migrations are applied before running tests."""
    close_pool()
    runner = MigrationRunner()
    runner.migrate()
    yield
    close_pool()


@pytest.fixture(autouse=True)
def clean_database():
    """Cleans up the database before and after each test."""
    def _truncate():
        with get_connection() as conn:
            with get_cursor(conn) as cur:
                cur.execute("ALTER TABLE interaction_ledger DISABLE TRIGGER trg_ledger_prevent_truncate;")
                cur.execute("ALTER TABLE interaction_ledger DISABLE TRIGGER trg_ledger_prevent_delete;")
                cur.execute("TRUNCATE TABLE interaction_ledger CASCADE;")
                cur.execute("TRUNCATE TABLE lead_rejections CASCADE;")
                cur.execute("TRUNCATE TABLE leads CASCADE;")
                cur.execute("TRUNCATE TABLE dispositions CASCADE;")
                cur.execute("TRUNCATE TABLE ingestion_file_checkpoints CASCADE;")
                cur.execute("TRUNCATE TABLE ingestion_runs CASCADE;")
                cur.execute("ALTER TABLE interaction_ledger ENABLE TRIGGER trg_ledger_prevent_truncate;")
                cur.execute("ALTER TABLE interaction_ledger ENABLE TRIGGER trg_ledger_prevent_delete;")
            conn.commit()

    _truncate()
    yield
    _truncate()


@pytest.fixture
def repo():
    return LeadRepository()


def _get_db_counts():
    """Helper to return row counts for leads, lead_rejections, checkpoints."""
    with get_connection() as conn:
        with get_cursor(conn) as cur:
            cur.execute("SELECT COUNT(*) FROM leads;")
            leads_cnt = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM lead_rejections;")
            rej_cnt = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM ingestion_file_checkpoints;")
            chk_cnt = cur.fetchone()[0]
            return leads_cnt, rej_cnt, chk_cnt


# ===========================================================================
# 4. Transaction and Checkpoint Atomicity (Failure Injection at 5 Stages)
# ===========================================================================

def test_failure_injection_1_during_rejection_persistence(tmp_path):
    """Failure injected during rejection persistence rolls back all changes."""
    leads_dir = tmp_path / "leads" / "motor_renewal"
    leads_dir.mkdir(parents=True)
    csv_file = leads_dir / "batch_001.csv"
    csv_file.write_text(
        "Insured Name,Mobile No,Policy No\n,9876543210,POL-1\n",
        encoding="utf-8",
    )

    with patch.object(LeadRepository, "record_rejections", side_effect=RuntimeError("Injected Rejection DB Crash")):
        with pytest.raises(RuntimeError, match="Injected Rejection DB Crash"):
            ingest_lead_csv(csv_file, drop_id="drop_fail_1")

    leads_cnt, rej_cnt, chk_cnt = _get_db_counts()
    assert (leads_cnt, rej_cnt, chk_cnt) == (0, 0, 0)


def test_failure_injection_2_before_lead_persistence(tmp_path):
    """Failure injected after rejections but before lead upsert rolls back everything."""
    leads_dir = tmp_path / "leads" / "motor_renewal"
    leads_dir.mkdir(parents=True)
    csv_file = leads_dir / "batch_001.csv"
    csv_file.write_text(
        "Insured Name,Mobile No,Policy No\nValid User,9876543210,POL-1\n,9876543211,POL-2\n",
        encoding="utf-8",
    )

    with patch.object(LeadRepository, "upsert_lead_record", side_effect=RuntimeError("Injected Before Lead Upsert")):
        with pytest.raises(RuntimeError, match="Injected Before Lead Upsert"):
            ingest_lead_csv(csv_file, drop_id="drop_fail_2")

    leads_cnt, rej_cnt, chk_cnt = _get_db_counts()
    assert (leads_cnt, rej_cnt, chk_cnt) == (0, 0, 0)


def test_failure_injection_3_during_lead_persistence(tmp_path):
    """Failure injected mid-way through multiple lead records rolls back all leads."""
    leads_dir = tmp_path / "leads" / "motor_renewal"
    leads_dir.mkdir(parents=True)
    csv_file = leads_dir / "batch_001.csv"
    csv_file.write_text(
        "Insured Name,Mobile No,Policy No\nUser 1,9876543210,POL-1\nUser 2,9876543211,POL-2\n",
        encoding="utf-8",
    )

    original_upsert = LeadRepository.upsert_lead_record
    call_count = 0

    def mock_upsert(self, lead_record, conn=None):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("Injected Crash on 2nd Lead")
        return original_upsert(self, lead_record, conn=conn)

    with patch.object(LeadRepository, "upsert_lead_record", side_effect=mock_upsert, autospec=True):
        with pytest.raises(RuntimeError, match="Injected Crash on 2nd Lead"):
            ingest_lead_csv(csv_file, drop_id="drop_fail_3")

    leads_cnt, rej_cnt, chk_cnt = _get_db_counts()
    assert (leads_cnt, rej_cnt, chk_cnt) == (0, 0, 0)


def test_failure_injection_4_before_checkpoint_insertion(tmp_path):
    """Failure injected after all leads & rejections processed, before checkpoint."""
    leads_dir = tmp_path / "leads" / "motor_renewal"
    leads_dir.mkdir(parents=True)
    csv_file = leads_dir / "batch_001.csv"
    csv_file.write_text(
        "Insured Name,Mobile No,Policy No\nUser 1,9876543210,POL-1\n",
        encoding="utf-8",
    )

    from contextlib import contextmanager
    from src.db.connection import get_cursor as real_get_cursor

    class CheckpointFailingCursor:
        def __init__(self, cur):
            self._cur = cur

        def execute(self, query, vars=None):
            if "INSERT INTO ingestion_file_checkpoints" in str(query):
                raise RuntimeError("Injected Checkpoint DB Crash")
            return self._cur.execute(query, vars)

        def __getattr__(self, name):
            return getattr(self._cur, name)

    @contextmanager
    def mock_get_cursor(conn):
        with real_get_cursor(conn) as cur:
            yield CheckpointFailingCursor(cur)

    with patch("src.ingestion.lead_pipeline.get_cursor", mock_get_cursor):
        with pytest.raises(RuntimeError, match="Injected Checkpoint DB Crash"):
            ingest_lead_csv(csv_file, drop_id="drop_fail_4")

    leads_cnt, rej_cnt, chk_cnt = _get_db_counts()
    assert (leads_cnt, rej_cnt, chk_cnt) == (0, 0, 0)


def test_failure_injection_5_before_commit(tmp_path):
    """Failure injected immediately before transaction commit rolls back entire state."""
    leads_dir = tmp_path / "leads" / "motor_renewal"
    leads_dir.mkdir(parents=True)
    csv_file = leads_dir / "batch_001.csv"
    csv_file.write_text(
        "Insured Name,Mobile No,Policy No\nUser 1,9876543210,POL-1\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Simulated outer commit failure"):
        with transaction() as conn:
            ingest_lead_csv(csv_file, drop_id="drop_fail_5", conn=conn)
            # Simulated failure right before context manager commits
            raise ValueError("Simulated outer commit failure")

    leads_cnt, rej_cnt, chk_cnt = _get_db_counts()
    assert (leads_cnt, rej_cnt, chk_cnt) == (0, 0, 0)


# ===========================================================================
# 5. Verification of All Six Idempotency Scenarios
# ===========================================================================

def test_idempotency_scenario_1_identical_file_reingestion(tmp_path, repo):
    """Scenario 1: Identical file re-ingestion in same drop skips cleanly with zero mutations."""
    leads_dir = tmp_path / "leads" / "motor_renewal"
    leads_dir.mkdir(parents=True)
    csv_file = leads_dir / "batch_001.csv"
    csv_file.write_text(
        "Insured Name,Mobile No,Policy No\nUser 1,9876543210,POL-1\n",
        encoding="utf-8",
    )

    s1 = ingest_lead_csv(csv_file, drop_id="drop_sc1")
    assert s1.status == "PROCESSED"
    assert s1.inserted_leads == 1
    assert repo.count_leads("motor_renewal") == 1

    s2 = ingest_lead_csv(csv_file, drop_id="drop_sc1")
    assert s2.status == "SKIPPED"
    assert s2.inserted_leads == 0
    assert repo.count_leads("motor_renewal") == 1


def test_idempotency_scenario_2_same_filename_changed_content(tmp_path, repo):
    """Scenario 2: Same filename with changed content in same drop aborts with FileIntegrityViolationError."""
    leads_dir = tmp_path / "leads" / "motor_renewal"
    leads_dir.mkdir(parents=True)
    csv_file = leads_dir / "batch_001.csv"
    csv_file.write_text(
        "Insured Name,Mobile No,Policy No\nUser 1,9876543210,POL-1\n",
        encoding="utf-8",
    )

    s1 = ingest_lead_csv(csv_file, drop_id="drop_sc2")
    assert s1.status == "PROCESSED"

    # Mutate file
    csv_file.write_text(
        "Insured Name,Mobile No,Policy No\nUser 1 Tampered,9876543210,POL-1-MOD\n",
        encoding="utf-8",
    )

    with pytest.raises(FileIntegrityViolationError, match="File integrity violation"):
        ingest_lead_csv(csv_file, drop_id="drop_sc2")

    # State retains original data, no partial corrupted write
    lead = repo.find_by_phone("motor_renewal", "+919876543210")
    assert repo.decrypt_customer_name(lead) == "User 1"
    assert lead.external_ref == "POL-1"


def test_idempotency_scenario_3_renamed_identical_file(tmp_path, repo):
    """Scenario 3: Renamed identical file processed in filename order merges deterministically."""
    leads_dir = tmp_path / "leads" / "motor_renewal"
    leads_dir.mkdir(parents=True)
    csv1 = leads_dir / "batch_001.csv"
    csv2 = leads_dir / "batch_002.csv"

    content = "Insured Name,Mobile No,Policy No,Premium\nUser 1,9876543210,POL-1,5000\n"
    csv1.write_text(content, encoding="utf-8")
    csv2.write_text(content, encoding="utf-8")

    # Process batch_001 first
    s1 = ingest_lead_csv(csv1, drop_id="drop_sc3")
    assert s1.status == "PROCESSED"
    assert s1.inserted_leads == 1

    lead1 = repo.find_by_phone("motor_renewal", "+919876543210")
    assert lead1.batch_id == "batch_001"

    # Process batch_002 (different filename, identical lead content)
    s2 = ingest_lead_csv(csv2, drop_id="drop_sc3")
    assert s2.status == "PROCESSED"
    assert s2.inserted_leads == 0
    assert s2.updated_leads == 1
    assert s2.duplicate_rows_merged == 1

    # Invariant: permanent batch_id remains locked to first processed file stem
    lead2 = repo.find_by_phone("motor_renewal", "+919876543210")
    assert lead2.batch_id == "batch_001"
    assert repo.count_leads("motor_renewal") == 1


def test_idempotency_scenario_4_identical_content_in_another_drop(tmp_path, repo):
    """Scenario 4: Identical content in another drop merges non-null fields and preserves batch_id."""
    drop1_dir = tmp_path / "drop_1" / "leads" / "motor_renewal"
    drop1_dir.mkdir(parents=True)
    csv1 = drop1_dir / "batch_001.csv"
    csv1.write_text(
        "Insured Name,Mobile No,Policy No,Premium,Preferred Language\nKaran Johar,9876543290,POL-KJ,10000,ta\n",
        encoding="utf-8",
    )

    drop2_dir = tmp_path / "drop_2" / "leads" / "motor_renewal"
    drop2_dir.mkdir(parents=True)
    csv2 = drop2_dir / "batch_003.csv"
    # Drop 2 has updated premium and omits language
    csv2.write_text(
        "Insured Name,Mobile No,Policy No,Premium,Preferred Language\nKaran Johar,9876543290,POL-KJ,15000,\n",
        encoding="utf-8",
    )

    s1 = ingest_lead_csv(csv1, drop_id="drop_1")
    assert s1.inserted_leads == 1

    s2 = ingest_lead_csv(csv2, drop_id="drop_2")
    assert s2.inserted_leads == 0
    assert s2.updated_leads == 1
    assert s2.duplicate_rows_merged == 1

    lead = repo.find_by_phone("motor_renewal", "+919876543290")
    # Invariant: batch_id permanently locked to batch_001
    assert lead.batch_id == "batch_001"
    # Invariant: non-null updated premium
    assert lead.premium_amount == Decimal("15000.00")
    # Invariant: existing user language 'ta' NOT overwritten by default 'en'
    assert lead.preferred_language == "ta"


def test_idempotency_scenario_5_crash_before_commit_leaves_clean_state(tmp_path, repo):
    """Scenario 5: Crash before commit leaves zero partial records in PostgreSQL."""
    leads_dir = tmp_path / "leads" / "motor_renewal"
    leads_dir.mkdir(parents=True)
    csv_file = leads_dir / "batch_001.csv"
    csv_file.write_text(
        "Insured Name,Mobile No,Policy No\nUser 5,9876543295,POL-5\n",
        encoding="utf-8",
    )

    # Ingest within transaction that simulates crash before commit
    with pytest.raises(SystemExit):
        with transaction() as conn:
            ingest_lead_csv(csv_file, drop_id="drop_sc5", conn=conn)
            raise SystemExit(1)

    leads_cnt, rej_cnt, chk_cnt = _get_db_counts()
    assert (leads_cnt, rej_cnt, chk_cnt) == (0, 0, 0)
    assert repo.find_by_phone("motor_renewal", "+919876543295") is None


def test_idempotency_scenario_6_crash_after_commit_restart(tmp_path, repo):
    """Scenario 6: Crash after commit allows safe restart without duplicate writes."""
    leads_dir = tmp_path / "leads" / "motor_renewal"
    leads_dir.mkdir(parents=True)
    csv_file = leads_dir / "batch_001.csv"
    csv_file.write_text(
        "Insured Name,Mobile No,Policy No\nUser 6,9876543296,POL-6\n",
        encoding="utf-8",
    )

    # Run 1 commits successfully
    s1 = ingest_lead_csv(csv_file, drop_id="drop_sc6")
    assert s1.status == "PROCESSED"
    assert s1.inserted_leads == 1

    # Process terminates. On restart, pipeline re-runs the same drop
    s2 = ingest_lead_csv(csv_file, drop_id="drop_sc6")
    assert s2.status == "SKIPPED"
    assert s2.inserted_leads == 0

    assert repo.count_leads("motor_renewal") == 1
