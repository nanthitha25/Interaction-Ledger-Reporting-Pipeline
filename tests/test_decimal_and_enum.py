"""
Unit tests for decimal currency and enum normalizers (Task LEAD-03).
Validates rules specified in FR-LD-008, FR-LD-009, BR-005, and BR-006.
"""
from decimal import Decimal
import json
from pathlib import Path
import pytest

from src.ingestion.normalizers import (
    InvalidDecimalError,
    InvalidEnumError,
    normalize_decimal,
    normalize_enum,
)


class TestDecimalNormalization:
    """Test suite for normalize_decimal()."""

    # --------------------------------------------------------------------------
    # 1. Integers, Decimals, and Zero
    # --------------------------------------------------------------------------

    @pytest.mark.parametrize(
        "raw_input,expected",
        [
            ("15000", Decimal("15000.00")),
            ("15000.5", Decimal("15000.50")),
            ("15000.50", Decimal("15000.50")),
            ("0", Decimal("0.00")),
            ("0.0", Decimal("0.00")),
            ("0.00", Decimal("0.00")),
            ("1000000000.99", Decimal("1000000000.99")),
        ],
    )
    def test_basic_numeric_formats(self, raw_input: str, expected: Decimal):
        assert normalize_decimal(raw_input) == expected

    # --------------------------------------------------------------------------
    # 2. Currency Prefixes (₹, Rs, Rs., INR) & Casing
    # --------------------------------------------------------------------------

    @pytest.mark.parametrize(
        "raw_input,expected",
        [
            ("₹15000", Decimal("15000.00")),
            ("₹ 15000.50", Decimal("15000.50")),
            ("₹0", Decimal("0.00")),
            ("Rs 15000", Decimal("15000.00")),
            ("rs 15000", Decimal("15000.00")),
            ("RS 15000", Decimal("15000.00")),
            ("Rs. 15000.50", Decimal("15000.50")),
            ("rs. 15000.50", Decimal("15000.50")),
            ("RS. 15000.50", Decimal("15000.50")),
            ("Rs.15000.50", Decimal("15000.50")),
            ("INR 1200", Decimal("1200.00")),
            ("inr 1200", Decimal("1200.00")),
            ("Inr 1200.75", Decimal("1200.75")),
            ("inr1200", Decimal("1200.00")),
        ],
    )
    def test_currency_prefixes(self, raw_input: str, expected: Decimal):
        assert normalize_decimal(raw_input) == expected

    # --------------------------------------------------------------------------
    # 3. Comma Thousands Separators
    # --------------------------------------------------------------------------

    @pytest.mark.parametrize(
        "raw_input,expected",
        [
            ("15,000.50", Decimal("15000.50")),
            ("₹15,000.00", Decimal("15000.00")),
            ("₹ 1,23,456.78", Decimal("123456.78")),
            ("INR 1,234,567.89", Decimal("1234567.89")),
            ("Rs. 50,000", Decimal("50000.00")),
        ],
    )
    def test_comma_separated_values(self, raw_input: str, expected: Decimal):
        assert normalize_decimal(raw_input) == expected

    # --------------------------------------------------------------------------
    # 4. Whitespace Handling
    # --------------------------------------------------------------------------

    @pytest.mark.parametrize(
        "raw_input,expected",
        [
            ("  ₹  15000.50  ", Decimal("15000.50")),
            ("\tRs. 1200.00\n", Decimal("1200.00")),
            ("   INR   999.99   ", Decimal("999.99")),
        ],
    )
    def test_whitespace_handling(self, raw_input: str, expected: Decimal):
        assert normalize_decimal(raw_input) == expected

    # --------------------------------------------------------------------------
    # 5. Rounding Behaviour (Strict Half-Up to 2 places)
    # --------------------------------------------------------------------------

    @pytest.mark.parametrize(
        "raw_input,expected",
        [
            ("15000.555", Decimal("15000.56")),
            ("15000.554", Decimal("15000.55")),
            ("12.341", Decimal("12.34")),
            ("12.349", Decimal("12.35")),
            ("₹ 99.999", Decimal("100.00")),
        ],
    )
    def test_rounding_to_two_places(self, raw_input: str, expected: Decimal):
        assert normalize_decimal(raw_input) == expected

    # --------------------------------------------------------------------------
    # 6. Negative Values (Must be Rejected)
    # --------------------------------------------------------------------------

    @pytest.mark.parametrize(
        "negative_input",
        [
            "-500",
            "-500.00",
            "₹ -10",
            "Rs. -250.50",
            "INR -0.01",
            "-₹100",
        ],
    )
    def test_negative_values_rejected(self, negative_input: str):
        with pytest.raises(InvalidDecimalError) as exc_info:
            normalize_decimal(negative_input)
        assert "non-negative" in str(exc_info.value) or "could not parse" in str(exc_info.value)

    # --------------------------------------------------------------------------
    # 7. Malformed and Invalid Inputs
    # --------------------------------------------------------------------------

    @pytest.mark.parametrize(
        "malformed_input",
        [
            "abc",
            "₹",
            "Rs.",
            "INR",
            "$100",           # Dollar prefix not allowed
            "12.34.56",       # Multiple decimal points
            "₹ 15,000.50.00",
            "N/A",
            "FREE",
        ],
    )
    def test_malformed_values_rejected(self, malformed_input: str):
        with pytest.raises(InvalidDecimalError):
            normalize_decimal(malformed_input)

    # --------------------------------------------------------------------------
    # 8. Empty, Whitespace, None and Non-string Inputs
    # --------------------------------------------------------------------------

    def test_empty_string_rejected(self):
        with pytest.raises(InvalidDecimalError):
            normalize_decimal("")

    def test_whitespace_string_rejected(self):
        with pytest.raises(InvalidDecimalError):
            normalize_decimal("   ")

    def test_none_input_rejected(self):
        with pytest.raises(InvalidDecimalError):
            normalize_decimal(None)

    def test_non_string_input_rejected(self):
        with pytest.raises(InvalidDecimalError):
            normalize_decimal(15000)  # type: ignore


class TestEnumNormalization:
    """Test suite for normalize_enum()."""

    ALLOWED_LANGUAGES = ["en", "hi", "ta", "kn", "te", "mr"]

    # --------------------------------------------------------------------------
    # 1. Exact, Upper, Lower, and Mixed Case
    # --------------------------------------------------------------------------

    @pytest.mark.parametrize(
        "raw_input,expected",
        [
            ("en", "en"),
            ("EN", "en"),
            ("En", "en"),
            ("eN", "en"),
            ("hi", "hi"),
            ("HI", "hi"),
            ("Hi", "hi"),
            ("TA", "ta"),
            ("Kn", "kn"),
            ("TE", "te"),
            ("mr", "mr"),
        ],
    )
    def test_case_insensitive_matching(self, raw_input: str, expected: str):
        assert normalize_enum(raw_input, self.ALLOWED_LANGUAGES) == expected

    def test_surrounding_whitespace(self):
        assert normalize_enum("  EN  ", self.ALLOWED_LANGUAGES) == "en"
        assert normalize_enum("\thi\n", self.ALLOWED_LANGUAGES) == "hi"

    # --------------------------------------------------------------------------
    # 2. Invalid Values
    # --------------------------------------------------------------------------

    @pytest.mark.parametrize(
        "invalid_value",
        [
            "fr",          # French (not in schema)
            "es",          # Spanish
            "english",     # Full name instead of code
            "hindi",
            "unknown",
            "123",
        ],
    )
    def test_invalid_enum_rejected(self, invalid_value: str):
        with pytest.raises(InvalidEnumError) as exc_info:
            normalize_enum(invalid_value, self.ALLOWED_LANGUAGES)
        assert "permitted values" in str(exc_info.value)

    # --------------------------------------------------------------------------
    # 3. Empty, Whitespace, None, and Non-string Inputs
    # --------------------------------------------------------------------------

    def test_empty_enum_rejected(self):
        with pytest.raises(InvalidEnumError):
            normalize_enum("", self.ALLOWED_LANGUAGES)

    def test_whitespace_enum_rejected(self):
        with pytest.raises(InvalidEnumError):
            normalize_enum("   ", self.ALLOWED_LANGUAGES)

    def test_none_enum_rejected(self):
        with pytest.raises(InvalidEnumError):
            normalize_enum(None, self.ALLOWED_LANGUAGES)

    def test_non_string_enum_rejected(self):
        with pytest.raises(InvalidEnumError):
            normalize_enum(1, self.ALLOWED_LANGUAGES)  # type: ignore

    # --------------------------------------------------------------------------
    # 4. Arbitrary / Reusable Enum Sets
    # --------------------------------------------------------------------------

    def test_custom_allowed_sets(self):
        custom_status = ["ACTIVE", "INACTIVE", "Pending"]
        assert normalize_enum("active", custom_status) == "active"
        assert normalize_enum("PENDING", custom_status) == "pending"
        assert normalize_enum("inactive", custom_status) == "inactive"
        with pytest.raises(InvalidEnumError):
            normalize_enum("deleted", custom_status)

    # --------------------------------------------------------------------------
    # 5. Schema-Driven Tests (from actual schema JSON files)
    # --------------------------------------------------------------------------

    def test_schema_driven_language_validation(self):
        schema_path = Path(__file__).parent.parent / "schemas" / "motor_renewal.json"
        with open(schema_path, "r", encoding="utf-8") as f:
            motor_schema = json.load(f)

        # Find preferred_language field
        lang_field = next(f for f in motor_schema["fields"] if f["name"] == "preferred_language")
        allowed_from_schema = lang_field["values"]

        for valid_lang in allowed_from_schema:
            assert normalize_enum(valid_lang.upper(), allowed_from_schema) == valid_lang

        with pytest.raises(InvalidEnumError):
            normalize_enum("german", allowed_from_schema)
