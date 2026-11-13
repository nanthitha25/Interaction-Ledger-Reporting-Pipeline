"""
Integration Test Suite for Lead Ingestion Pipeline.

Tests end-to-end integration across CSV validation, header mapping,
intra-batch and cross-batch deduplication, file checkpoints, and
PostgreSQL LeadRepository persistence.
"""

import datetime
from decimal import Decimal
from pathlib import Path
import pytest

from src.db.connection import close_pool, get_connection, get_cursor
from src.db.migrations.runner import MigrationRunner
from src.ingestion.lead_pipeline import (
    FileIntegrityViolationError,
    compute_file_sha256,
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


def test_motor_renewal_lead_csv_ingestion(tmp_path, repo):
    # Setup test CSV file under /leads/motor_renewal/batch_001.csv
    leads_dir = tmp_path / "leads" / "motor_renewal"
    leads_dir.mkdir(parents=True)
    csv_file = leads_dir / "batch_001.csv"

    csv_content = """Insured Name,Mobile No,Policy No,Expiry Date,Premium,Language
Rahul Dravid,9876543210,POL-RD-101,31/12/2025,"₹15,000.50",kn
Sourav Ganguly,91-98765-43211,POL-SG-102,15-08-2026,INR 12500,hi
Sachin Tendulkar,09876543212,POL-ST-103,2026-05-20,18000.00,mr
"""
    csv_file.write_text(csv_content, encoding="utf-8")

    summary = ingest_lead_csv(
        file_path=csv_file,
        drop_id="drop_1",
    )

    assert summary.status == "PROCESSED"
    assert summary.total_nonblank_rows == 3
    assert summary.accepted_rows == 3
    assert summary.rejected_rows == 0
    assert summary.duplicate_rows_merged == 0
    assert summary.inserted_leads == 3
    assert summary.updated_leads == 0

    # Verify leads in database
    assert repo.count_leads("motor_renewal") == 3

    lead_rd = repo.find_by_phone("motor_renewal", "+919876543210")
    assert lead_rd is not None
    assert lead_rd.external_ref == "POL-RD-101"
    assert lead_rd.batch_id == "batch_001"
    assert lead_rd.policy_expiry == datetime.date(2025, 12, 31)
    assert lead_rd.premium_amount == Decimal("15000.50")
    assert lead_rd.preferred_language == "kn"
    assert repo.decrypt_customer_name(lead_rd) == "Rahul Dravid"


def test_rejections_and_warnings_tracking(tmp_path, repo):
    leads_dir = tmp_path / "leads" / "motor_renewal"
    leads_dir.mkdir(parents=True)
    csv_file = leads_dir / "batch_001.csv"

    # Row 1: Valid
    # Row 2: Missing customer name -> rejection (missing_required:customer_name)
    # Row 3: Invalid phone (landline) -> rejection (invalid_phone)
    # Row 4: Valid phone & name, but invalid date -> warning (date becomes NULL)
    csv_content = """Customer Name,Contact,Policy Number,Policy Expiry,Premium Amount,Language
Anil Kumble,9876543220,POL-AK-01,2025-10-10,5000,en
,9876543221,POL-INV-02,2025-10-10,5000,en
Javagal Srinath,022-2430-1234,POL-INV-03,2025-10-10,5000,en
VVS Laxman,9876543223,POL-VL-04,not-a-valid-date,6000,te
"""
    csv_file.write_text(csv_content, encoding="utf-8")

    summary = ingest_lead_csv(
        file_path=csv_file,
        drop_id="drop_test_rej",
    )

    assert summary.total_nonblank_rows == 4
    assert summary.accepted_rows == 2  # Rows 1 and 4
    assert summary.rejected_rows == 2  # Rows 2 and 3
    assert summary.warning_count == 1  # Row 4 bad date
    assert summary.inserted_leads == 2

    # Check rejection breakdown
    assert summary.rejections_by_reason.get("missing_required:customer_name") == 1
    assert summary.rejections_by_reason.get("invalid_phone") == 1

    # Check rejections persisted in PostgreSQL
    db_rejections = repo.get_rejections(drop_id="drop_test_rej")
    assert len(db_rejections) == 2
    assert db_rejections[0]["row_number"] == 2
    assert db_rejections[0]["reason"] == "missing_required:customer_name"
    assert db_rejections[1]["row_number"] == 3
    assert db_rejections[1]["reason"] == "invalid_phone"

    # Verify warned lead has NULL policy_expiry but was accepted
    warned_lead = repo.find_by_phone("motor_renewal", "+919876543223")
    assert warned_lead is not None
    assert warned_lead.policy_expiry is None
    assert warned_lead.preferred_language == "te"


def test_intra_batch_and_cross_batch_deduplication(tmp_path, repo):
    # Batch 1 in Drop 1: has 2 rows for the same customer (intra-batch deduplication)
    drop1_dir = tmp_path / "drop_1" / "leads" / "motor_renewal"
    drop1_dir.mkdir(parents=True)
    csv1 = drop1_dir / "batch_001.csv"

    # Row 1: sets language 'ta', premium 5000
    # Row 2: same phone, omits language, updates premium to 7000
    csv1_content = """Customer Name,Phone Number,Policy Number,Expiry Date,Premium,Preferred Language
MS Dhoni,9876543250,POL-MSD-01,2025-11-01,5000,ta
MS Dhoni,9876543250,POL-MSD-01,2025-11-01,7000,
"""
    csv1.write_text(csv1_content, encoding="utf-8")

    summary1 = ingest_lead_csv(
        file_path=csv1,
        drop_id="drop_1",
    )

    assert summary1.total_nonblank_rows == 2
    assert summary1.accepted_rows == 2
    assert summary1.duplicate_rows_merged == 1
    assert summary1.inserted_leads == 1
    assert summary1.updated_leads == 0

    lead_drop1 = repo.find_by_phone("motor_renewal", "+919876543250")
    assert lead_drop1.batch_id == "batch_001"
    assert lead_drop1.preferred_language == "ta"
    assert lead_drop1.premium_amount == Decimal("7000.00")

    # Batch 2 in Drop 2: customer appears again in batch_002 (cross-batch deduplication)
    # Incoming row omits language and omits policy expiry, but provides updated premium 8500
    drop2_dir = tmp_path / "drop_2" / "leads" / "motor_renewal"
    drop2_dir.mkdir(parents=True)
    csv2 = drop2_dir / "batch_002.csv"

    csv2_content = """Customer Name,Phone Number,Policy Number,Expiry Date,Premium,Preferred Language
MS Dhoni Updated,9876543250,POL-MSD-01,,8500,
"""
    csv2.write_text(csv2_content, encoding="utf-8")

    summary2 = ingest_lead_csv(
        file_path=csv2,
        drop_id="drop_2",
    )

    assert summary2.total_nonblank_rows == 1
    assert summary2.accepted_rows == 1
    assert summary2.inserted_leads == 0
    assert summary2.updated_leads == 1
    assert summary2.duplicate_rows_merged == 1

    lead_drop2 = repo.find_by_phone("motor_renewal", "+919876543250")
    # Invariant: Permanent batch_id retained
    assert lead_drop2.batch_id == "batch_001"
    # Invariant: Existing user language 'ta' NOT overwritten by default 'en'
    assert lead_drop2.preferred_language == "ta"
    # Invariant: Existing non-null policy_expiry preserved when incoming is null
    assert lead_drop2.policy_expiry == datetime.date(2025, 11, 1)
    # Invariant: Non-null incoming premium updated
    assert lead_drop2.premium_amount == Decimal("8500.00")
    # Invariant: Customer name updated
    assert repo.decrypt_customer_name(lead_drop2) == "MS Dhoni Updated"


def test_file_idempotency_scenarios_1_and_2(tmp_path):
    leads_dir = tmp_path / "leads" / "motor_renewal"
    leads_dir.mkdir(parents=True)
    csv_file = leads_dir / "batch_001.csv"

    content = """Customer Name,Phone,Policy Number
Rohit Sharma,9876543230,POL-RS-45
"""
    csv_file.write_text(content, encoding="utf-8")

    # 1. First run: successfully processes
    s1 = ingest_lead_csv(csv_file, drop_id="drop_idem")
    assert s1.status == "PROCESSED"
    assert s1.inserted_leads == 1

    # 2. Scenario 1: Identical file re-ingested in same drop -> SKIPPED
    s2 = ingest_lead_csv(csv_file, drop_id="drop_idem")
    assert s2.status == "SKIPPED"

    # 3. Scenario 2: File mutated with different content under same filename -> ABORT
    mutated_content = """Customer Name,Phone,Policy Number
Rohit Sharma,9876543230,POL-RS-45-MODIFIED
"""
    csv_file.write_text(mutated_content, encoding="utf-8")

    with pytest.raises(FileIntegrityViolationError, match="File integrity violation"):
        ingest_lead_csv(csv_file, drop_id="drop_idem")


def test_policy_feedback_workflow_ingestion(tmp_path, repo):
    leads_dir = tmp_path / "leads" / "policy_feedback"
    leads_dir.mkdir(parents=True)
    csv_file = leads_dir / "batch_001.csv"

    csv_content = """Customer Name,Mobile No,Order ID,Item,Purchase Date,Language
Yuvraj Singh,9876543240,ORD-YS-12,Laptop Extended Warranty,15/07/2025,hi
Zaheer Khan,9876543241,ORD-ZK-34,Screen Damage Cover,2025-06-10,
"""
    csv_file.write_text(csv_content, encoding="utf-8")

    summary = ingest_lead_csv(
        file_path=csv_file,
        drop_id="drop_feedback",
    )

    assert summary.status == "PROCESSED"
    assert summary.total_nonblank_rows == 2
    assert summary.accepted_rows == 2
    assert summary.inserted_leads == 2

    lead_1 = repo.find_by_phone("policy_feedback", "+919876543240")
    assert lead_1.external_ref == "ORD-YS-12"
    assert lead_1.product == "Laptop Extended Warranty"
    assert lead_1.order_date == datetime.date(2025, 7, 15)
    assert lead_1.preferred_language == "hi"

    lead_2 = repo.find_by_phone("policy_feedback", "+919876543241")
    assert lead_2.external_ref == "ORD-ZK-34"
    assert lead_2.product == "Screen Damage Cover"
    assert lead_2.order_date == datetime.date(2025, 6, 10)
    assert lead_2.preferred_language == "en"  # Schema default applied for new lead
