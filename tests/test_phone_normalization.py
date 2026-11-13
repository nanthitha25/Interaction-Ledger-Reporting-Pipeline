"""
Unit tests for Indian mobile phone normalization (Task LEAD-04).
Validates rules specified in FR-LD-006 and BR-003.
"""
import pytest

from src.ingestion.normalizers import InvalidPhoneError, normalize_indian_phone


class TestIndianPhoneNormalization:
    """Test suite for normalize_indian_phone."""

    @pytest.mark.parametrize(
        "raw_input,expected_canonical",
        [
            # 1. Plain 10 digits
            ("9876543210", "+919876543210"),
            ("8123456789", "+918123456789"),
            ("7718750036", "+917718750036"),
            ("6123456789", "+916123456789"),

            # 2. Leading +91 with spaces, dashes, brackets
            ("+91 98765 43210", "+919876543210"),
            ("+91-77187-50036", "+917718750036"),
            ("(+91) 8123456789", "+918123456789"),
            ("+919876543210", "+919876543210"),
            ("  +91  61234  56789  ", "+916123456789"),

            # 3. Leading 91 without plus (12 digits total)
            ("919876543210", "+919876543210"),
            ("91-77187-50036", "+917718750036"),
            ("91 81234 56789", "+918123456789"),

            # 4. Leading 0 (11 digits total)
            ("09876543210", "+919876543210"),
            ("0-77187-50036", "+917718750036"),
            ("0 81234 56789", "+918123456789"),
            ("06123456789", "+916123456789"),

            # 5. Mixed formatting
            ("  (91) 98765-43210 ", "+919876543210"),
        ],
    )
    def test_canonical_phone_success(self, raw_input: str, expected_canonical: str):
        assert normalize_indian_phone(raw_input) == expected_canonical

    @pytest.mark.parametrize(
        "invalid_input,reason",
        [
            # Invalid starting digits for mobile (5, 4, 3, 2, 1, 0)
            ("5123456789", "Starts with 5"),
            ("4123456789", "Starts with 4"),
            ("3123456789", "Starts with 3"),
            ("2123456789", "Starts with 2"),
            ("1234567890", "Starts with 1"),
            ("+91 51234 56789", "Starts with 5 after +91"),
            ("05123456789", "Starts with 5 after dropping leading 0"),

            # Too few digits (< 10)
            ("987654321", "9 digits"),
            ("12345", "5 digits"),
            ("", "Empty string"),
            ("   ", "Whitespace only"),

            # Too many digits (> 10 after prefix removal)
            ("9198765432100", "13 digits starting with 91 (leaves 11 digits)"),
            ("098765432100", "12 digits with leading 0 (leaves 11 digits)"),
            ("98765432101", "11 digits not starting with 0"),

            # 11 digits starting with 91 (not 12 digits, so 91 is NOT dropped, leaving 11 digits)
            ("91876543210", "11 digits starting with 91"),

            # Landline with 022 STD code (Mumbai landline from generator: 022-2430-1234)
            ("022-2430-1234", "Dropping 0 leaves 2224301234 which starts with 2"),
            ("033-2430-1234", "Dropping 0 leaves 3324301234 which starts with 3"),
            ("044-2430-1234", "Dropping 0 leaves 4424301234 which starts with 4"),

            # International numbers
            ("+1 415 555 0134", "US number (11 digits not starting with 0)"),
            ("+44 7911 123456", "UK number"),

            # Non-digits / Alpha
            ("N/A", "Text string"),
            ("INVALID_PHONE", "Text string"),
            ("98765ABCDE", "Contains alphabets"),
        ],
    )
    def test_invalid_phone_rejections(self, invalid_input: str, reason: str):
        with pytest.raises(InvalidPhoneError):
            normalize_indian_phone(invalid_input)

    def test_none_input_raises_invalid_phone_error(self):
        with pytest.raises(InvalidPhoneError):
            normalize_indian_phone(None)

    def test_non_string_input_raises_invalid_phone_error(self):
        with pytest.raises(InvalidPhoneError):
            normalize_indian_phone(1234567890)  # type: ignore

    def test_exception_does_not_leak_plaintext_phone(self):
        secret_test_phone = "5999988887"
        try:
            normalize_indian_phone(secret_test_phone)
            pytest.fail("Expected InvalidPhoneError")
        except InvalidPhoneError as e:
            # Exception message must NOT include the raw sensitive phone number
            assert secret_test_phone not in str(e)
