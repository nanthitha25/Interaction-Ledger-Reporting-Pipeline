"""
Unit tests for Schema-Order Validation & Rejection Engine (Task LEAD-06).
Validates rules specified in FR-LD-005, FR-LD-010, FR-LD-011, BR-007, and BR-008.
"""
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import pytest

from src.ingestion.headers import build_header_mapping
from src.ingestion.row_reader import CSVRow, read_csv_rows
from src.ingestion.validator import (
    AcceptedLeadRow,
    RejectedLeadRow,
    ValidationBatchResult,
    load_workflow_schema,
    validate_lead_file,
    validate_lead_row,
    validate_lead_rows,
)


@pytest.fixture
def motor_schema() -> dict:
    schema_path = Path(__file__).parent.parent / "schemas" / "motor_renewal.json"
    return load_workflow_schema(schema_path)


@pytest.fixture
def feedback_schema() -> dict:
    schema_path = Path(__file__).parent.parent / "schemas" / "policy_feedback.json"
    return load_workflow_schema(schema_path)


class TestLeadValidationEngine:
    """Test suite for schema-order validation and rejection engine."""

    # 1. Valid row with all fields
    def test_1_valid_row_with_all_fields(self, motor_schema: dict):
        csv_headers = ["customer_name", "phone", "policy_number", "policy_expiry", "premium_amount", "preferred_language"]
        mapping = build_header_mapping(csv_headers, motor_schema)
        row = CSVRow(row_number=1, values=["Aarav Sharma", "9876543210", "POL-123456", "01-Dec-2026", "₹ 15,000.50", "en"])

        result = validate_lead_row(row, mapping, motor_schema["fields"])

        assert isinstance(result, AcceptedLeadRow)
        assert result.row_number == 1
        assert result.warning_count == 0
        assert result.values["customer_name"] == "Aarav Sharma"
        assert result.values["phone"] == "+919876543210"
        assert result.values["policy_number"] == "POL-123456"
        assert result.values["policy_expiry"] == date(2026, 12, 1)
        assert result.values["premium_amount"] == Decimal("15000.50")
        assert result.values["preferred_language"] == "en"

    # 2. Missing required field
    def test_2_missing_required_field(self, motor_schema: dict):
        csv_headers = ["customer_name", "phone", "policy_number"]
        mapping = build_header_mapping(csv_headers, motor_schema)
        # customer_name is empty string
        row = CSVRow(row_number=1, values=["", "9876543210", "POL-123456"])

        result = validate_lead_row(row, mapping, motor_schema["fields"])

        assert isinstance(result, RejectedLeadRow)
        assert result.row_number == 1
        assert result.reason == "missing_required:customer_name"

    # 3. Invalid required phone
    def test_3_invalid_required_phone(self, motor_schema: dict):
        csv_headers = ["customer_name", "phone", "policy_number"]
        mapping = build_header_mapping(csv_headers, motor_schema)
        row = CSVRow(row_number=1, values=["John Doe", "5123456789", "POL-123456"])

        result = validate_lead_row(row, mapping, motor_schema["fields"])

        assert isinstance(result, RejectedLeadRow)
        assert result.row_number == 1
        assert result.reason == "invalid_phone"

    # 4. Invalid required date
    def test_4_invalid_required_date(self):
        custom_schema = [
            {"name": "event_date", "type": "date", "required": True},
        ]
        mapping = build_header_mapping(["event_date"], custom_schema)
        row = CSVRow(row_number=1, values=["31/02/2026"])

        result = validate_lead_row(row, mapping, custom_schema)

        assert isinstance(result, RejectedLeadRow)
        assert result.row_number == 1
        assert result.reason == "invalid_date"

    # 5. Invalid required decimal
    def test_5_invalid_required_decimal(self):
        custom_schema = [
            {"name": "fee", "type": "decimal", "required": True},
        ]
        mapping = build_header_mapping(["fee"], custom_schema)
        row = CSVRow(row_number=1, values=["₹ -500"])

        result = validate_lead_row(row, mapping, custom_schema)

        assert isinstance(result, RejectedLeadRow)
        assert result.row_number == 1
        assert result.reason == "invalid_decimal"

    # 6. Invalid required enum
    def test_6_invalid_required_enum(self):
        custom_schema = [
            {"name": "status", "type": "enum", "required": True, "values": ["active", "closed"]},
        ]
        mapping = build_header_mapping(["status"], custom_schema)
        row = CSVRow(row_number=1, values=["pending"])

        result = validate_lead_row(row, mapping, custom_schema)

        assert isinstance(result, RejectedLeadRow)
        assert result.row_number == 1
        assert result.reason == "invalid_enum"

    # 7. Invalid optional decimal becomes None
    def test_7_invalid_optional_decimal_becomes_none(self, motor_schema: dict):
        csv_headers = ["customer_name", "phone", "policy_number", "premium_amount"]
        mapping = build_header_mapping(csv_headers, motor_schema)
        # premium_amount is optional, value is malformed text
        row = CSVRow(row_number=1, values=["Aarav", "9876543210", "POL-123", "N/A"])

        result = validate_lead_row(row, mapping, motor_schema["fields"])

        assert isinstance(result, AcceptedLeadRow)
        assert result.values["premium_amount"] is None
        assert result.warning_count == 1

    # 8. Invalid optional date becomes None
    def test_8_invalid_optional_date_becomes_none(self, motor_schema: dict):
        csv_headers = ["customer_name", "phone", "policy_number", "policy_expiry"]
        mapping = build_header_mapping(csv_headers, motor_schema)
        # policy_expiry is optional, date is impossible calendar date
        row = CSVRow(row_number=1, values=["Aarav", "9876543210", "POL-123", "31/02/2026"])

        result = validate_lead_row(row, mapping, motor_schema["fields"])

        assert isinstance(result, AcceptedLeadRow)
        assert result.values["policy_expiry"] is None
        assert result.warning_count == 1

    # 9. Optional missing field produces no warning
    def test_9_optional_missing_field_produces_no_warning(self, motor_schema: dict):
        csv_headers = ["customer_name", "phone", "policy_number", "policy_expiry", "premium_amount"]
        mapping = build_header_mapping(csv_headers, motor_schema)
        # policy_expiry and premium_amount are empty
        row = CSVRow(row_number=1, values=["Aarav", "9876543210", "POL-123", "", ""])

        result = validate_lead_row(row, mapping, motor_schema["fields"])

        assert isinstance(result, AcceptedLeadRow)
        assert result.values["policy_expiry"] is None
        assert result.values["premium_amount"] is None
        assert result.warning_count == 0

    # 10. Multiple invalid fields produce only the first rejection
    def test_10_multiple_invalid_fields_produce_only_first_rejection(self, motor_schema: dict):
        csv_headers = ["customer_name", "phone", "policy_number"]
        mapping = build_header_mapping(csv_headers, motor_schema)
        # Both customer_name (missing) and phone (invalid) fail
        row = CSVRow(row_number=1, values=["", "12345", "POL-123"])

        result = validate_lead_row(row, mapping, motor_schema["fields"])

        assert isinstance(result, RejectedLeadRow)
        # First field in motor_schema is customer_name
        assert result.reason == "missing_required:customer_name"

    # 11. Schema order determines rejection reason
    def test_11_schema_order_determines_rejection_reason(self):
        # Define schema where phone is before name
        phone_first_schema = [
            {"name": "phone", "type": "phone", "required": True},
            {"name": "customer_name", "type": "string", "required": True},
        ]
        mapping = build_header_mapping(["phone", "customer_name"], phone_first_schema)
        # Both fail
        row = CSVRow(row_number=1, values=["bad_phone", ""])

        result = validate_lead_row(row, mapping, phone_first_schema)

        assert isinstance(result, RejectedLeadRow)
        # First in schema order is phone
        assert result.reason == "invalid_phone"

    # 12. Valid enum values are loaded dynamically from schema
    def test_12_dynamic_enum_validation(self, feedback_schema: dict):
        csv_headers = ["customer_name", "phone", "order_id", "preferred_language"]
        mapping = build_header_mapping(csv_headers, feedback_schema)
        row = CSVRow(row_number=1, values=["Diya", "9876543210", "ORD-123", "TA"])

        result = validate_lead_row(row, mapping, feedback_schema["fields"])

        assert isinstance(result, AcceptedLeadRow)
        assert result.values["preferred_language"] == "ta"

    # 13. Unknown CSV columns are ignored
    def test_13_unknown_csv_columns_ignored(self, motor_schema: dict):
        csv_headers = ["internal_id", "customer_name", "random_notes", "phone", "policy_number"]
        mapping = build_header_mapping(csv_headers, motor_schema)
        row = CSVRow(row_number=1, values=["ID-999", "Aarav", "Notes here", "9876543210", "POL-100"])

        result = validate_lead_row(row, mapping, motor_schema["fields"])

        assert isinstance(result, AcceptedLeadRow)
        assert "internal_id" not in result.values
        assert "random_notes" not in result.values
        assert result.values["customer_name"] == "Aarav"

    # 14. Header mapping works with aliases
    def test_14_header_mapping_aliases(self, motor_schema: dict):
        csv_headers = ["Insured Name", "Mobile", "Policy No", "Language"]
        mapping = build_header_mapping(csv_headers, motor_schema)
        row = CSVRow(row_number=1, values=["Karthik", "9876543210", "MOT/2026/001", "HI"])

        result = validate_lead_row(row, mapping, motor_schema["fields"])

        assert isinstance(result, AcceptedLeadRow)
        assert result.values["customer_name"] == "Karthik"
        assert result.values["phone"] == "+919876543210"
        assert result.values["policy_number"] == "MOT/2026/001"
        assert result.values["preferred_language"] == "hi"

    # 15. Blank rows are skipped by pipeline
    def test_15_blank_rows_not_validated(self, tmp_path: Path, motor_schema: dict):
        content = (
            "customer_name,phone,policy_number\n"
            "Aarav,9876543210,POL-1\n"
            "\n"
            "   \n"
            "Jane,9123456789,POL-2\n"
        )
        csv_file = tmp_path / "leads.csv"
        csv_file.write_text(content, encoding="utf-8")

        mapping, batch_result = validate_lead_file(csv_file, motor_schema)

        assert batch_result.total_nonblank_rows == 2
        assert batch_result.accepted_count == 2
        assert batch_result.rejected_count == 0
        assert [r.row_number for r in batch_result.accepted_rows] == [1, 2]

    # 16. Warning counts are correct
    def test_16_warning_counts_correct(self, motor_schema: dict):
        csv_headers = ["customer_name", "phone", "policy_number", "policy_expiry", "premium_amount", "preferred_language"]
        mapping = build_header_mapping(csv_headers, motor_schema)
        # 2 invalid optional fields: policy_expiry ("bad_date") and premium_amount ("bad_num")
        row = CSVRow(row_number=1, values=["Aarav", "9876543210", "POL-1", "bad_date", "bad_num", "en"])

        result = validate_lead_row(row, mapping, motor_schema["fields"])

        assert isinstance(result, AcceptedLeadRow)
        assert result.warning_count == 2
        assert result.values["policy_expiry"] is None
        assert result.values["premium_amount"] is None

    # 17. Rejected rows do not contribute optional-field warnings
    def test_17_rejected_rows_do_not_contribute_warnings(self, motor_schema: dict):
        csv_headers = ["customer_name", "phone", "policy_number", "policy_expiry"]
        mapping = build_header_mapping(csv_headers, motor_schema)
        # Row 1 has invalid phone (rejected) AND invalid date
        row1 = CSVRow(row_number=1, values=["Aarav", "invalid_phone", "POL-1", "bad_date"])
        # Row 2 is valid with invalid optional date (accepted with 1 warning)
        row2 = CSVRow(row_number=2, values=["Jane", "9876543210", "POL-2", "bad_date"])

        batch_result = validate_lead_rows([row1, row2], mapping, motor_schema)

        assert batch_result.total_nonblank_rows == 2
        assert batch_result.accepted_count == 1
        assert batch_result.rejected_count == 1
        assert batch_result.rejected_rows[0].reason == "invalid_phone"
        # Total warnings should only count the accepted row (1 warning)
        assert batch_result.total_warnings == 1
        assert batch_result.rejections_by_reason == {"invalid_phone": 1}
