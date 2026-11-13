"""
Unit tests for Lead Deduplication & Merging Engine (Task LEAD-07).
Validates rules specified in FR-LD-012, FR-LD-013, FR-LD-014, BR-009, BR-010, BR-011, and BR-012.
"""
from decimal import Decimal
import json
from pathlib import Path
import pytest

from src.ingestion.deduplication import (
    DeduplicationResult,
    LeadDeduplicationEngine,
    LeadIdentity,
    LeadRecord,
    apply_schema_defaults,
    deduplicate_lead_batches,
    merge_lead_values,
)
from src.ingestion.validator import AcceptedLeadRow, RejectedLeadRow, load_workflow_schema


@pytest.fixture
def motor_schema() -> dict:
    schema_path = Path(__file__).parent.parent / "schemas" / "motor_renewal.json"
    return load_workflow_schema(schema_path)


class TestLeadDeduplication:
    """Test suite for lead deduplication and merging engine."""

    # 1. First valid lead creates a new record
    def test_1_first_valid_lead_creates_record(self):
        engine = LeadDeduplicationEngine()
        row = AcceptedLeadRow(
            row_number=1,
            values={"customer_name": "Aarav", "phone": "+919876543210", "policy_number": "POL-1"},
            warning_count=0,
        )

        record, is_new = engine.process_accepted_row("motor_renewal", "batch_001", row)

        assert is_new is True
        assert record.batch_id == "batch_001"
        assert record.phone == "+919876543210"
        assert record.values["customer_name"] == "Aarav"
        assert record.row_count == 1
        assert engine.duplicate_count == 0

    # 2. Same phone and same workflow merges into one lead
    def test_2_same_phone_same_workflow_merges(self):
        engine = LeadDeduplicationEngine()
        row1 = AcceptedLeadRow(
            row_number=1,
            values={"customer_name": "Aarav", "phone": "+919876543210", "premium_amount": Decimal("10000.00")},
            warning_count=0,
        )
        row2 = AcceptedLeadRow(
            row_number=2,
            values={"customer_name": "Aarav Sharma", "phone": "+919876543210", "premium_amount": Decimal("12000.00")},
            warning_count=0,
        )

        rec1, is_new1 = engine.process_accepted_row("motor_renewal", "batch_001", row1)
        rec2, is_new2 = engine.process_accepted_row("motor_renewal", "batch_002", row2)

        assert is_new1 is True
        assert is_new2 is False
        assert rec1 is rec2
        assert len(engine.leads) == 1
        assert engine.duplicate_count == 1
        assert rec1.row_count == 2
        # Last-write-wins updated the values
        assert rec1.values["customer_name"] == "Aarav Sharma"
        assert rec1.values["premium_amount"] == Decimal("12000.00")

    # 3. Same phone and different workflow creates separate leads
    def test_3_same_phone_different_workflow_separate_leads(self):
        engine = LeadDeduplicationEngine()
        row_motor = AcceptedLeadRow(
            row_number=1,
            values={"customer_name": "Aarav", "phone": "+919876543210", "policy_number": "POL-1"},
            warning_count=0,
        )
        row_feedback = AcceptedLeadRow(
            row_number=1,
            values={"customer_name": "Aarav", "phone": "+919876543210", "order_id": "ORD-1"},
            warning_count=0,
        )

        rec_m, is_new_m = engine.process_accepted_row("motor_renewal", "batch_001", row_motor)
        rec_f, is_new_f = engine.process_accepted_row("policy_feedback", "batch_001", row_feedback)

        assert is_new_m is True
        assert is_new_f is True
        assert len(engine.leads) == 2
        assert engine.duplicate_count == 0
        assert rec_m.identity == LeadIdentity("motor_renewal", "+919876543210")
        assert rec_f.identity == LeadIdentity("policy_feedback", "+919876543210")

    # 4. Different phone and same workflow creates separate leads
    def test_4_different_phone_same_workflow_separate_leads(self):
        engine = LeadDeduplicationEngine()
        row1 = AcceptedLeadRow(row_number=1, values={"customer_name": "Aarav", "phone": "+919876543210"}, warning_count=0)
        row2 = AcceptedLeadRow(row_number=2, values={"customer_name": "Diya", "phone": "+919123456789"}, warning_count=0)

        engine.process_accepted_row("motor_renewal", "batch_001", row1)
        engine.process_accepted_row("motor_renewal", "batch_001", row2)

        assert len(engine.leads) == 2
        assert engine.duplicate_count == 0

    # 5. Later non-null value overwrites the earlier value
    def test_5_later_non_null_overwrites_earlier_value(self):
        engine = LeadDeduplicationEngine()
        row1 = AcceptedLeadRow(
            row_number=1,
            values={"phone": "+919876543210", "customer_name": "Aarav", "premium_amount": Decimal("10000.00")},
            warning_count=0,
        )
        row2 = AcceptedLeadRow(
            row_number=2,
            values={"phone": "+919876543210", "customer_name": "Aarav Sharma", "premium_amount": Decimal("15000.00")},
            warning_count=0,
        )

        engine.process_accepted_row("motor_renewal", "batch_001", row1)
        record, _ = engine.process_accepted_row("motor_renewal", "batch_002", row2)

        assert record.values["customer_name"] == "Aarav Sharma"
        assert record.values["premium_amount"] == Decimal("15000.00")

    # 6. Later null value does not overwrite an earlier non-null value
    def test_6_later_null_does_not_overwrite_earlier_non_null(self):
        engine = LeadDeduplicationEngine()
        row1 = AcceptedLeadRow(
            row_number=1,
            values={"phone": "+919876543210", "customer_name": "Aarav", "premium_amount": Decimal("10000.00")},
            warning_count=0,
        )
        # In batch_002, premium_amount is None
        row2 = AcceptedLeadRow(
            row_number=2,
            values={"phone": "+919876543210", "customer_name": "Aarav Sharma", "premium_amount": None},
            warning_count=0,
        )

        engine.process_accepted_row("motor_renewal", "batch_001", row1)
        record, _ = engine.process_accepted_row("motor_renewal", "batch_002", row2)

        # Name is updated, but premium_amount retains Decimal("10000.00")
        assert record.values["customer_name"] == "Aarav Sharma"
        assert record.values["premium_amount"] == Decimal("10000.00")

    # 7. Original batch_id remains unchanged after merging
    def test_7_permanent_batch_id_preserved(self):
        engine = LeadDeduplicationEngine()
        row1 = AcceptedLeadRow(row_number=1, values={"phone": "+919876543210", "customer_name": "Aarav"}, warning_count=0)
        row2 = AcceptedLeadRow(row_number=2, values={"phone": "+919876543210", "customer_name": "Aarav Sharma"}, warning_count=0)
        row3 = AcceptedLeadRow(row_number=3, values={"phone": "+919876543210", "customer_name": "Aarav S."}, warning_count=0)

        engine.process_accepted_row("motor_renewal", "batch_001", row1)
        engine.process_accepted_row("motor_renewal", "batch_002", row2)
        record, _ = engine.process_accepted_row("motor_renewal", "batch_003", row3)

        assert record.batch_id == "batch_001"
        assert record.row_count == 3
        assert engine.duplicate_count == 2

    # 8. Multiple duplicates merge in deterministic order
    def test_8_multiple_duplicates_merge_in_order(self):
        engine = LeadDeduplicationEngine()
        names = ["Aarav", "Aarav S.", "Aarav Sharma", "Mr. Aarav Sharma"]
        for idx, name in enumerate(names, start=1):
            row = AcceptedLeadRow(row_number=idx, values={"phone": "+919876543210", "customer_name": name}, warning_count=0)
            engine.process_accepted_row("motor_renewal", f"batch_{idx:03d}", row)

        record = engine.leads[LeadIdentity("motor_renewal", "+919876543210")]
        assert record.values["customer_name"] == "Mr. Aarav Sharma"
        assert record.batch_id == "batch_001"
        assert record.row_count == 4
        assert engine.duplicate_count == 3

    # 9. Filename ordering is respected
    def test_9_filename_ordering_respected(self):
        # batch_001 -> batch_002 -> batch_003
        batches = [
            ("motor_renewal", "batch_001", [
                AcceptedLeadRow(1, {"phone": "+919876543210", "customer_name": "Batch 1 Name"}, 0)
            ]),
            ("motor_renewal", "batch_002", [
                AcceptedLeadRow(1, {"phone": "+919876543210", "customer_name": "Batch 2 Name"}, 0)
            ]),
            ("motor_renewal", "batch_003", [
                AcceptedLeadRow(1, {"phone": "+919876543210", "customer_name": "Batch 3 Name"}, 0)
            ]),
        ]
        res = deduplicate_lead_batches(batches)

        lead = res.leads[LeadIdentity("motor_renewal", "+919876543210")]
        assert lead.batch_id == "batch_001"
        assert lead.values["customer_name"] == "Batch 3 Name"

    # 10. Row ordering is respected
    def test_10_row_ordering_respected(self):
        rows = [
            AcceptedLeadRow(1, {"phone": "+919876543210", "customer_name": "Row 1"}, 0),
            AcceptedLeadRow(2, {"phone": "+919876543210", "customer_name": "Row 2"}, 0),
            AcceptedLeadRow(3, {"phone": "+919876543210", "customer_name": "Row 3"}, 0),
        ]
        res = deduplicate_lead_batches([("motor_renewal", "batch_001", rows)])

        lead = res.leads[LeadIdentity("motor_renewal", "+919876543210")]
        assert lead.values["customer_name"] == "Row 3"
        assert lead.row_count == 3

    # 11. Defaults are applied after merging
    def test_11_defaults_applied_after_merging(self, motor_schema: dict):
        engine = LeadDeduplicationEngine(schema_fields=motor_schema["fields"])

        # Row 1: preferred_language is None
        row1 = AcceptedLeadRow(
            1,
            {"phone": "+919876543210", "customer_name": "Aarav", "preferred_language": None},
            0,
        )
        # Row 2: preferred_language is still None
        row2 = AcceptedLeadRow(
            2,
            {"phone": "+919876543210", "customer_name": "Aarav Sharma", "preferred_language": None},
            0,
        )

        engine.process_accepted_row("motor_renewal", "batch_001", row1)
        engine.process_accepted_row("motor_renewal", "batch_002", row2)

        # Before apply_defaults(), it is None
        assert engine.leads[LeadIdentity("motor_renewal", "+919876543210")].values["preferred_language"] is None

        # Apply defaults
        engine.apply_defaults()

        # After apply_defaults(), schema default "en" is applied
        assert engine.leads[LeadIdentity("motor_renewal", "+919876543210")].values["preferred_language"] == "en"

    def test_11b_defaults_do_not_overwrite_merged_values(self, motor_schema: dict):
        engine = LeadDeduplicationEngine(schema_fields=motor_schema["fields"])

        # Row 1 has None, Row 2 has "hi"
        row1 = AcceptedLeadRow(1, {"phone": "+919876543210", "preferred_language": None}, 0)
        row2 = AcceptedLeadRow(2, {"phone": "+919876543210", "preferred_language": "hi"}, 0)

        engine.process_accepted_row("motor_renewal", "batch_001", row1)
        engine.process_accepted_row("motor_renewal", "batch_002", row2)
        engine.apply_defaults()

        # Explicit non-null "hi" must NOT be overwritten by default "en"
        assert engine.leads[LeadIdentity("motor_renewal", "+919876543210")].values["preferred_language"] == "hi"

    # 12. Invalid/rejected rows never enter the deduplication engine
    def test_12_rejected_rows_excluded(self):
        # Simulates pipeline integration where validator filters out RejectedLeadRow
        engine = LeadDeduplicationEngine()
        valid_row = AcceptedLeadRow(1, {"phone": "+919876543210", "customer_name": "Aarav"}, 0)
        rejected_row = RejectedLeadRow(2, "invalid_phone")

        # In pipeline, only AcceptedLeadRow is passed to deduplication
        if isinstance(valid_row, AcceptedLeadRow):
            engine.process_accepted_row("motor_renewal", "batch_001", valid_row)
        if isinstance(rejected_row, AcceptedLeadRow):
            engine.process_accepted_row("motor_renewal", "batch_001", rejected_row)  # pragma: no cover

        assert len(engine.leads) == 1
        assert engine.duplicate_count == 0

    # 13. Phone identity uses canonical phone numbers
    def test_13_canonical_phone_identity(self):
        engine = LeadDeduplicationEngine()
        # Two rows with identical canonical phone (+919876543210)
        row1 = AcceptedLeadRow(1, {"phone": "+919876543210", "name": "John"}, 0)
        row2 = AcceptedLeadRow(2, {"phone": "+919876543210", "name": "John Doe"}, 0)

        engine.process_accepted_row("motor_renewal", "batch_001", row1)
        engine.process_accepted_row("motor_renewal", "batch_002", row2)

        assert len(engine.leads) == 1
        assert engine.duplicate_count == 1

    # 14. An empty dataset produces no leads
    def test_14_empty_dataset_produces_no_leads(self):
        res = deduplicate_lead_batches([])
        assert res.total_leads == 0
        assert res.duplicate_count == 0
        assert res.get_leads_list() == []

    # 15. Duplicate counts are accurate
    def test_15_duplicate_counts_accurate(self):
        engine = LeadDeduplicationEngine()
        # 3 rows for phone A (1 initial + 2 duplicates)
        # 2 rows for phone B (1 initial + 1 duplicate)
        # 1 row for phone C (1 initial + 0 duplicates)
        rows_a = [
            AcceptedLeadRow(1, {"phone": "+919000000001", "name": "A1"}, 0),
            AcceptedLeadRow(2, {"phone": "+919000000001", "name": "A2"}, 0),
            AcceptedLeadRow(3, {"phone": "+919000000001", "name": "A3"}, 0),
        ]
        rows_b = [
            AcceptedLeadRow(4, {"phone": "+919000000002", "name": "B1"}, 0),
            AcceptedLeadRow(5, {"phone": "+919000000002", "name": "B2"}, 0),
        ]
        rows_c = [
            AcceptedLeadRow(6, {"phone": "+919000000003", "name": "C1"}, 0),
        ]

        for r in rows_a + rows_b + rows_c:
            engine.process_accepted_row("motor_renewal", "batch_001", r)

        res = engine.get_result()
        assert res.total_leads == 3
        # 2 dups from A + 1 dup from B = 3 duplicates
        assert res.duplicate_count == 3
        assert res.leads[LeadIdentity("motor_renewal", "+919000000001")].row_count == 3
        assert res.leads[LeadIdentity("motor_renewal", "+919000000002")].row_count == 2
        assert res.leads[LeadIdentity("motor_renewal", "+919000000003")].row_count == 1
