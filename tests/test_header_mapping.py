"""
Unit tests for header normalization and schema-driven alias matching (Task LEAD-01).
Validates rules specified in FR-LD-002 and BR-001.
"""
import json
from pathlib import Path
import pytest

from src.ingestion.headers import (
    AmbiguousHeaderError,
    HeaderMapping,
    HeaderMappingError,
    build_header_mapping,
    normalize_header,
)


class TestHeaderNormalization:
    """Tests for normalize_header()."""

    @pytest.mark.parametrize(
        "raw_header,expected_normalized",
        [
            ("Customer Name", "customer name"),
            ("customer_name", "customer name"),
            ("CUSTOMER_NAME", "customer name"),
            ("  customer_name  ", "customer name"),
            ("customer   name", "customer name"),
            ("customer___name", "customer name"),
            ("  _ _ customer _ _ name _ _ ", "customer name"),
            ("POLICY_NUMBER", "policy number"),
            ("policy no", "policy no"),
            ("mobile_no", "mobile no"),
            ("  Phone  Number  ", "phone number"),
            ("___", ""),
            ("   ", ""),
            ("", ""),
            (None, ""),
        ],
    )
    def test_normalize_header_variations(self, raw_header: str | None, expected_normalized: str):
        assert normalize_header(raw_header) == expected_normalized


class TestBuildHeaderMapping:
    """Tests for build_header_mapping()."""

    @pytest.fixture
    def motor_schema(self) -> dict:
        schema_path = Path(__file__).parent.parent / "schemas" / "motor_renewal.json"
        with open(schema_path, "r", encoding="utf-8") as f:
            return json.load(f)

    @pytest.fixture
    def feedback_schema(self) -> dict:
        schema_path = Path(__file__).parent.parent / "schemas" / "policy_feedback.json"
        with open(schema_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_canonical_motor_headers(self, motor_schema: dict):
        csv_headers = [
            "customer_name",
            "phone",
            "policy_number",
            "policy_expiry",
            "premium_amount",
            "preferred_language",
        ]
        mapping = build_header_mapping(csv_headers, motor_schema)

        assert isinstance(mapping, HeaderMapping)
        assert len(mapping) == 6
        assert mapping[0] == "customer_name"
        assert mapping[1] == "phone"
        assert mapping[2] == "policy_number"
        assert mapping[3] == "policy_expiry"
        assert mapping[4] == "premium_amount"
        assert mapping[5] == "preferred_language"

    def test_motor_aliases_with_varied_casing_and_underscores(self, motor_schema: dict):
        csv_headers = [
            "  INSURED NAME  ",
            "Mobile No",
            "POLICY_NO",
            "Expiry Date",
            "PREMIUM",
            "Language",
        ]
        mapping = build_header_mapping(csv_headers, motor_schema)

        assert mapping[0] == "customer_name"
        assert mapping[1] == "phone"
        assert mapping[2] == "policy_number"
        assert mapping[3] == "policy_expiry"
        assert mapping[4] == "premium_amount"
        assert mapping[5] == "preferred_language"

        # Verify access by raw header name
        assert mapping["  INSURED NAME  "] == "customer_name"
        assert mapping["Mobile No"] == "phone"
        assert mapping["POLICY_NO"] == "policy_number"

        # Verify access by field name
        assert mapping.get_index_by_field("customer_name") == 0
        assert mapping.get_index_by_field("policy_number") == 2

    def test_reordered_columns_and_unknown_columns(self, feedback_schema: dict):
        csv_headers = [
            "extra_col_1",
            "Purchase Date",     # alias for order_date
            "unknown_metadata",
            "Customer Name",     # customer_name
            "Contact",           # alias for phone
            "Item",              # alias for product
            "ORDER",             # alias for order_id
        ]
        mapping = build_header_mapping(csv_headers, feedback_schema)

        # Mapped count is 5 (order_date, customer_name, phone, product, order_id)
        assert len(mapping) == 5

        # Check column indices
        assert mapping[1] == "order_date"
        assert mapping[3] == "customer_name"
        assert mapping[4] == "phone"
        assert mapping[5] == "product"
        assert mapping[6] == "order_id"

        # Check unmapped columns
        assert mapping.unmapped_indices == [0, 2]
        assert mapping.unmapped_headers == ["extra_col_1", "unknown_metadata"]

    def test_ambiguous_csv_mapping_duplicate_targets(self, motor_schema: dict):
        # Both "Name" and "Insured Name" map to "customer_name"
        csv_headers = ["Name", "phone", "Insured Name"]
        with pytest.raises(AmbiguousHeaderError) as exc_info:
            build_header_mapping(csv_headers, motor_schema)
        assert "Ambiguous CSV headers" in str(exc_info.value)
        assert "customer_name" in str(exc_info.value)

    def test_duplicate_exact_headers(self, motor_schema: dict):
        # Two columns with identical name mapping to the same field
        csv_headers = ["phone", "policy_number", "phone"]
        with pytest.raises(AmbiguousHeaderError) as exc_info:
            build_header_mapping(csv_headers, motor_schema)
        assert "phone" in str(exc_info.value)

    def test_ambiguous_schema_alias_conflict(self):
        conflicting_schema = [
            {"name": "field_a", "aliases": ["shared_alias"]},
            {"name": "field_b", "aliases": ["SHARED_ALIAS"]},
        ]
        csv_headers = ["shared_alias"]
        with pytest.raises(AmbiguousHeaderError) as exc_info:
            build_header_mapping(csv_headers, conflicting_schema)
        assert "Schema definition conflict" in str(exc_info.value)

    def test_missing_required_fields_helper(self, motor_schema: dict):
        # Provide only customer_name and phone; policy_number (required) is missing
        csv_headers = ["customer_name", "phone", "premium_amount"]
        mapping = build_header_mapping(csv_headers, motor_schema)

        missing = mapping.get_missing_required_fields(motor_schema["fields"])
        assert "policy_number" in missing
        assert "customer_name" not in missing
        assert "phone" not in missing

    def test_invalid_input_types(self, motor_schema: dict):
        with pytest.raises(HeaderMappingError):
            build_header_mapping("not_a_list", motor_schema)  # type: ignore

        with pytest.raises(HeaderMappingError):
            build_header_mapping(["col1"], "not_a_schema")  # type: ignore
