"""
Unit and Integration Test Suite for LeadRepository.

Tests all persistence requirements:
1. Insert new lead into PostgreSQL.
2. Find existing lead using (workflow_id, phone_bidx) and helpers.
3. Update existing leads using last-non-null-write-wins semantics.
4. Preserve the original batch_id permanently.
5. Encrypt PII before database insertion.
6. Never persist plaintext phone numbers or customer names.
7. Apply schema defaults at the correct stage (never overwrite explicit value with default).
8. Execute writes transactionally (atomic rollbacks).
9. Verify uniqueness constraints and concurrency behavior.
10. Integration with LeadRecord and lead_rejections persistence.
"""

from concurrent.futures import ThreadPoolExecutor
import datetime
from decimal import Decimal
import pytest
import psycopg2
from psycopg2 import errors

from src.db.connection import close_pool, get_connection, get_cursor, transaction
from src.db.migrations.runner import MigrationRunner
from src.ingestion.deduplication import LeadIdentity, LeadRecord
from src.ingestion.validator import RejectedLeadRow
from src.repository.lead_repository import (
    LeadModel,
    LeadRepository,
    LeadRepositoryError,
    LeadValidationError,
)
from src.security.vault import PIIVault


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
    """Returns a fresh LeadRepository instance."""
    return LeadRepository()


# ---------------------------------------------------------------------------
# 1. Insert a new lead into PostgreSQL
# ---------------------------------------------------------------------------
def test_insert_new_lead(repo):
    lead, is_inserted = repo.upsert_lead(
        workflow_id="motor_renewal",
        batch_id="batch_001",
        phone="+919876543210",
        customer_name="Pooja Sharma",
        external_ref="POL-9901",
        policy_expiry=datetime.date(2025, 12, 31),
        premium_amount=Decimal("12500.50"),
    )

    assert is_inserted is True
    assert lead.lead_id is not None
    assert len(lead.lead_id) == 36  # Standard UUID length
    assert lead.workflow_id == "motor_renewal"
    assert lead.batch_id == "batch_001"
    assert lead.external_ref == "POL-9901"
    assert lead.policy_expiry == datetime.date(2025, 12, 31)
    assert lead.premium_amount == Decimal("12500.50")
    assert lead.preferred_language == "en"  # Schema default
    assert lead.created_at is not None
    assert lead.updated_at is not None

    # Verify count in database
    assert repo.count_leads("motor_renewal") == 1
    assert repo.count_leads() == 1


# ---------------------------------------------------------------------------
# 2. Find existing lead using (workflow_id, phone_bidx), phone, and ID
# ---------------------------------------------------------------------------
def test_find_existing_lead(repo):
    lead, _ = repo.upsert_lead(
        workflow_id="motor_renewal",
        batch_id="batch_001",
        phone="+919876543210",
        customer_name="Pooja Sharma",
        external_ref="POL-9901",
    )

    # Find by identity (workflow_id, phone_bidx)
    found_by_idty = repo.find_by_identity("motor_renewal", lead.phone_bidx)
    assert found_by_idty is not None
    assert found_by_idty.lead_id == lead.lead_id
    assert found_by_idty.phone_bidx == lead.phone_bidx

    # Find by phone
    found_by_phone = repo.find_by_phone("motor_renewal", "+919876543210")
    assert found_by_phone is not None
    assert found_by_phone.lead_id == lead.lead_id

    # Also accepts raw non-canonical phone format
    found_by_raw_phone = repo.find_by_phone("motor_renewal", "9876543210")
    assert found_by_raw_phone is not None
    assert found_by_raw_phone.lead_id == lead.lead_id

    # Find by primary key ID
    found_by_id = repo.find_by_id(lead.lead_id)
    assert found_by_id is not None
    assert found_by_id.lead_id == lead.lead_id

    # Non-existent lead returns None
    assert repo.find_by_phone("motor_renewal", "+919111111111") is None
    assert repo.find_by_identity("motor_renewal", "nonexistent_bidx" + "0" * 48) is None
    assert repo.find_by_id("00000000-0000-0000-0000-000000000000") is None


# ---------------------------------------------------------------------------
# 3. Update existing leads using last-non-null-write-wins semantics
# ---------------------------------------------------------------------------
def test_update_last_non_null_write_wins(repo):
    # Step 1: Initial insert with full optional fields
    lead_v1, is_inserted_1 = repo.upsert_lead(
        workflow_id="motor_renewal",
        batch_id="batch_001",
        phone="+919876543210",
        customer_name="Pooja Sharma",
        external_ref="POL-9901",
        policy_expiry=datetime.date(2025, 10, 15),
        premium_amount=Decimal("5000.00"),
    )
    assert is_inserted_1 is True
    assert lead_v1.premium_amount == Decimal("5000.00")
    assert lead_v1.policy_expiry == datetime.date(2025, 10, 15)

    # Step 2: Second drop updates premium_amount to 7500.00, but policy_expiry is NULL (None)
    lead_v2, is_inserted_2 = repo.upsert_lead(
        workflow_id="motor_renewal",
        batch_id="batch_002",
        phone="+919876543210",
        customer_name="Pooja Sharma",
        external_ref="POL-9901",
        policy_expiry=None,  # Missing in batch 2
        premium_amount=Decimal("7500.00"),  # Explicit new value
    )
    assert is_inserted_2 is False  # Existing lead updated
    assert lead_v2.lead_id == lead_v1.lead_id
    assert lead_v2.premium_amount == Decimal("7500.00")
    # Existing policy_expiry must be preserved when incoming value is NULL
    assert lead_v2.policy_expiry == datetime.date(2025, 10, 15)

    # Step 3: Third drop updates policy_expiry, but premium_amount is NULL (None)
    lead_v3, is_inserted_3 = repo.upsert_lead(
        workflow_id="motor_renewal",
        batch_id="batch_003",
        phone="+919876543210",
        customer_name="Pooja Sharma Updated",
        external_ref="POL-9901-REV",
        policy_expiry=datetime.date(2026, 1, 1),  # Explicit new date
        premium_amount=None,  # Missing in batch 3
    )
    assert is_inserted_3 is False
    assert lead_v3.lead_id == lead_v1.lead_id
    assert lead_v3.policy_expiry == datetime.date(2026, 1, 1)
    # Existing premium_amount must be preserved when incoming value is NULL
    assert lead_v3.premium_amount == Decimal("7500.00")
    assert lead_v3.external_ref == "POL-9901-REV"
    assert repo.decrypt_customer_name(lead_v3) == "Pooja Sharma Updated"


# ---------------------------------------------------------------------------
# 4. Preserve original batch_id permanently
# ---------------------------------------------------------------------------
def test_preserve_original_batch_id(repo):
    # Insert in batch_001
    lead_1, _ = repo.upsert_lead(
        workflow_id="motor_renewal",
        batch_id="batch_001",
        phone="+919876543210",
        customer_name="Pooja Sharma",
        external_ref="POL-9901",
    )
    assert lead_1.batch_id == "batch_001"

    # Subsequent update from batch_002
    lead_2, _ = repo.upsert_lead(
        workflow_id="motor_renewal",
        batch_id="batch_002",
        phone="+919876543210",
        customer_name="Pooja Sharma",
        external_ref="POL-9901",
        premium_amount=Decimal("15000.00"),
    )
    # batch_id must remain locked to the first batch
    assert lead_2.batch_id == "batch_001"

    # Subsequent update from batch_003
    lead_3, _ = repo.upsert_lead(
        workflow_id="motor_renewal",
        batch_id="batch_003",
        phone="+919876543210",
        customer_name="Pooja Sharma",
        external_ref="POL-9901",
    )
    assert lead_3.batch_id == "batch_001"

    # Confirm directly in PostgreSQL table
    direct = repo.find_by_id(lead_1.lead_id)
    assert direct.batch_id == "batch_001"


# ---------------------------------------------------------------------------
# 5. Encrypt PII before database insertion & round-trip verification
# ---------------------------------------------------------------------------
def test_encrypt_pii_before_insertion(repo):
    raw_phone = "+919876543210"
    raw_name = "Vikram Aditya"

    lead, _ = repo.upsert_lead(
        workflow_id="motor_renewal",
        batch_id="batch_001",
        phone=raw_phone,
        customer_name=raw_name,
        external_ref="POL-1111",
    )

    # 1. Blind index is 64 hex characters
    assert len(lead.phone_bidx) == 64
    assert lead.phone_bidx.isalnum()

    # 2. Ciphertext serialized format starts with 'v1:'
    assert lead.phone_encrypted.startswith("v1:")
    assert lead.customer_name_encrypted.startswith("v1:")

    # 3. Round-trip decryption through repository
    assert repo.decrypt_phone(lead) == raw_phone
    assert repo.decrypt_customer_name(lead) == raw_name

    # 4. Masked phone representation (+91******XXXX)
    assert repo.mask_phone(lead) == "+91******3210"


# ---------------------------------------------------------------------------
# 6. Never persist plaintext phone numbers or customer names
# ---------------------------------------------------------------------------
def test_never_persist_plaintext_pii(repo):
    raw_phone = "+919876543210"
    digits_only = "9876543210"
    raw_name = "Aravind Swamy"

    lead, _ = repo.upsert_lead(
        workflow_id="motor_renewal",
        batch_id="batch_001",
        phone=raw_phone,
        customer_name=raw_name,
        external_ref="POL-2222",
    )

    with get_connection() as conn:
        with get_cursor(conn) as cur:
            # Query the raw row as text from PostgreSQL
            cur.execute("SELECT leads::text FROM leads WHERE lead_id = %s;", (lead.lead_id,))
            raw_row_text = cur.fetchone()[0]

            # Plaintext phone numbers must NEVER appear in the raw stored row
            assert raw_phone not in raw_row_text
            assert digits_only not in raw_row_text

            # Plaintext customer name must NEVER appear in the raw stored row
            assert "Aravind" not in raw_row_text
            assert "Swamy" not in raw_row_text

            # Verify schema columns directly
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'leads';
                """
            )
            columns = {col[0] for col in cur.fetchall()}
            # Plaintext columns must NOT exist
            assert "phone" not in columns
            assert "customer_name" not in columns
            assert "name" not in columns
            # Secure columns MUST exist
            assert "phone_bidx" in columns
            assert "phone_encrypted" in columns
            assert "customer_name_encrypted" in columns


# ---------------------------------------------------------------------------
# 7. Apply schema defaults at correct stage (Never overwrite explicit with default)
# ---------------------------------------------------------------------------
def test_schema_defaults_semantics(repo):
    # Scenario A: New lead without language -> receives schema default 'en'
    lead_a, _ = repo.upsert_lead(
        workflow_id="motor_renewal",
        batch_id="batch_001",
        phone="+919876543201",
        customer_name="Customer A",
        external_ref="POL-A",
        preferred_language=None,  # Missing
    )
    assert lead_a.preferred_language == "en"

    # Scenario B: New lead with explicit language 'ta' (Tamil)
    lead_b, is_new_b = repo.upsert_lead(
        workflow_id="motor_renewal",
        batch_id="batch_001",
        phone="+919876543202",
        customer_name="Customer B",
        external_ref="POL-B",
        preferred_language="ta",  # Explicit user value
    )
    assert is_new_b is True
    assert lead_b.preferred_language == "ta"

    # Scenario C: Subsequent drop for Lead B omits language (preferred_language=None)
    # CRITICAL: The existing user preference 'ta' must NOT be overwritten by default 'en'!
    lead_b_updated, is_new_b2 = repo.upsert_lead(
        workflow_id="motor_renewal",
        batch_id="batch_002",
        phone="+919876543202",
        customer_name="Customer B",
        external_ref="POL-B",
        preferred_language=None,  # Omitted in batch 2
        premium_amount=Decimal("9999.00"),
    )
    assert is_new_b2 is False
    assert lead_b_updated.preferred_language == "ta"  # PRESERVED!

    # Scenario D: Subsequent drop for Lead B provides explicit new preference 'hi'
    # Last-write-wins successfully updates from 'ta' to 'hi'
    lead_b_updated_hi, _ = repo.upsert_lead(
        workflow_id="motor_renewal",
        batch_id="batch_003",
        phone="+919876543202",
        customer_name="Customer B",
        external_ref="POL-B",
        preferred_language="hi",  # Explicit new value
    )
    assert lead_b_updated_hi.preferred_language == "hi"


# ---------------------------------------------------------------------------
# 8. Execute writes transactionally
# ---------------------------------------------------------------------------
def test_transactional_rollback_on_failure(repo):
    # 1. Successful transaction commits multiple leads
    with transaction() as conn:
        repo.upsert_lead(
            workflow_id="motor_renewal",
            batch_id="batch_001",
            phone="+919876543211",
            customer_name="Lead One",
            external_ref="POL-T1",
            conn=conn,
        )
        repo.upsert_lead(
            workflow_id="motor_renewal",
            batch_id="batch_001",
            phone="+919876543212",
            customer_name="Lead Two",
            external_ref="POL-T2",
            conn=conn,
        )

    assert repo.count_leads("motor_renewal") == 2

    # 2. Transaction that raises an exception rolls back completely
    with pytest.raises(ValueError, match="Simulated mid-transaction crash"):
        with transaction() as conn:
            repo.upsert_lead(
                workflow_id="motor_renewal",
                batch_id="batch_001",
                phone="+919876543213",
                customer_name="Lead Three",
                external_ref="POL-T3",
                conn=conn,
            )
            raise ValueError("Simulated mid-transaction crash")

    # Lead Three must NOT exist in the database
    assert repo.count_leads("motor_renewal") == 2
    assert repo.find_by_phone("motor_renewal", "+919876543213") is None


# ---------------------------------------------------------------------------
# 9. Verify uniqueness constraints and concurrency behavior
# ---------------------------------------------------------------------------
def test_uniqueness_constraint_raw_insert():
    vault = PIIVault()
    phone = "+919876543210"
    bidx = vault.get_phone_blind_index(phone)
    enc_phone = vault.encrypt_phone(phone)
    enc_name = vault.encrypt_customer_name("Test Customer")

    with get_connection() as conn:
        with get_cursor(conn) as cur:
            # 1. Insert lead directly
            cur.execute(
                """
                INSERT INTO leads (
                    workflow_id, phone_bidx, phone_encrypted, customer_name_encrypted,
                    external_ref, batch_id
                ) VALUES ('motor_renewal', %s, %s, %s, 'REF1', 'batch_001');
                """,
                (bidx, enc_phone, enc_name),
            )
        conn.commit()

        # 2. Second direct INSERT with same (workflow_id, phone_bidx) without ON CONFLICT must fail
        with get_cursor(conn) as cur:
            with pytest.raises(errors.UniqueViolation):
                cur.execute(
                    """
                    INSERT INTO leads (
                        workflow_id, phone_bidx, phone_encrypted, customer_name_encrypted,
                        external_ref, batch_id
                    ) VALUES ('motor_renewal', %s, %s, %s, 'REF2', 'batch_002');
                    """,
                    (bidx, enc_phone, enc_name),
                )
        conn.rollback()


def test_concurrency_safe_upsert(repo):
    # Multiple concurrent threads upserting the exact same lead simultaneously
    # ON CONFLICT (workflow_id, phone_bidx) must guarantee zero duplicate errors
    phone = "+919876543299"

    def _worker(thread_idx: int):
        return repo.upsert_lead(
            workflow_id="motor_renewal",
            batch_id=f"batch_{thread_idx:03d}",
            phone=phone,
            customer_name=f"Customer Thread {thread_idx}",
            external_ref=f"POL-TH-{thread_idx}",
            premium_amount=Decimal(str(1000 + thread_idx * 100)),
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_worker, i) for i in range(16)]
        results = [f.result() for f in futures]

    assert len(results) == 16
    # Exactly 1 insertion, 15 updates
    inserted_flags = [r[1] for r in results]
    assert inserted_flags.count(True) == 1
    assert inserted_flags.count(False) == 15

    # Exactly 1 lead persisted in DB
    assert repo.count_leads("motor_renewal") == 1


# ---------------------------------------------------------------------------
# 10. Upsert LeadRecord objects & Rejections tracking
# ---------------------------------------------------------------------------
def test_upsert_lead_record_and_batch(repo):
    # Create LeadRecord instances as output by deduplication engine
    record_1 = LeadRecord(
        identity=LeadIdentity(workflow_id="motor_renewal", canonical_phone="+919876543221"),
        batch_id="batch_001",
        values={
            "customer_name": "Rohan Mehra",
            "policy_number": "POL-RM-01",
            "policy_expiry": datetime.date(2025, 9, 30),
            "premium_amount": Decimal("11200.00"),
            "preferred_language": "mr",
        },
    )

    record_2 = LeadRecord(
        identity=LeadIdentity(workflow_id="policy_feedback", canonical_phone="+919876543222"),
        batch_id="batch_001",
        values={
            "customer_name": "Kavita Rao",
            "order_id": "ORD-KR-99",
            "product": "Smartphone Shield",
            "order_date": datetime.date(2025, 8, 1),
            "preferred_language": "kn",
        },
    )

    results = repo.upsert_lead_records([record_1, record_2])
    assert len(results) == 2
    assert results[0][1] is True  # record_1 inserted
    assert results[1][1] is True  # record_2 inserted

    # Verify motor_renewal lead
    lead_1 = repo.find_by_phone("motor_renewal", "+919876543221")
    assert lead_1 is not None
    assert lead_1.external_ref == "POL-RM-01"
    assert lead_1.preferred_language == "mr"
    assert repo.decrypt_customer_name(lead_1) == "Rohan Mehra"

    # Verify policy_feedback lead
    lead_2 = repo.find_by_phone("policy_feedback", "+919876543222")
    assert lead_2 is not None
    assert lead_2.external_ref == "ORD-KR-99"
    assert lead_2.product == "Smartphone Shield"
    assert lead_2.preferred_language == "kn"
    assert repo.decrypt_customer_name(lead_2) == "Kavita Rao"


def test_record_and_retrieve_rejections(repo):
    # Seed ingestion run first (required by foreign key)
    with transaction() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO ingestion_runs (drop_id, ingest_order, status)
                VALUES ('drop_rej_test', 10, 'IN_PROGRESS')
                ON CONFLICT (drop_id) DO NOTHING;
                """
            )

    rejections = [
        RejectedLeadRow(row_number=3, reason="missing_required:customer_name"),
        RejectedLeadRow(row_number=7, reason="invalid_phone"),
        RejectedLeadRow(row_number=12, reason="invalid_enum:preferred_language"),
    ]

    count = repo.record_rejections(
        drop_id="drop_rej_test",
        workflow_id="motor_renewal",
        batch_id="batch_001",
        rejections=rejections,
    )
    assert count == 3

    saved_rejections = repo.get_rejections(drop_id="drop_rej_test", batch_id="batch_001")
    assert len(saved_rejections) == 3
    assert saved_rejections[0]["row_number"] == 3
    assert saved_rejections[0]["reason"] == "missing_required:customer_name"
    assert saved_rejections[1]["row_number"] == 7
    assert saved_rejections[1]["reason"] == "invalid_phone"
    assert saved_rejections[2]["row_number"] == 12
    assert saved_rejections[2]["reason"] == "invalid_enum:preferred_language"
