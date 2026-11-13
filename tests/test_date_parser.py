"""
Unit tests for day-first date parser (Task LEAD-02).
Validates rules specified in FR-LD-007 and BR-004.
"""
from datetime import date
import pytest

from src.ingestion.normalizers import InvalidDateError, parse_date


class TestDateParser:
    """Test suite for parse_date()."""

    # --------------------------------------------------------------------------
    # 1. All Four Supported Formats
    # --------------------------------------------------------------------------

    def test_format_1_yyyy_mm_dd(self):
        assert parse_date("2026-12-01") == date(2026, 12, 1)
        assert parse_date("2025-01-31") == date(2025, 1, 31)
        assert parse_date("2024-02-29") == date(2024, 2, 29)

    def test_format_2_dd_slash_mm_slash_yyyy(self):
        assert parse_date("01/12/2026") == date(2026, 12, 1)
        assert parse_date("31/01/2025") == date(2025, 1, 31)
        assert parse_date("29/02/2024") == date(2024, 2, 29)

    def test_format_3_dd_dash_mm_dash_yyyy(self):
        assert parse_date("01-12-2026") == date(2026, 12, 1)
        assert parse_date("31-01-2025") == date(2025, 1, 31)
        assert parse_date("29-02-2024") == date(2024, 2, 29)

    def test_format_4_dd_mon_yyyy(self):
        assert parse_date("01-Dec-2026") == date(2026, 12, 1)
        assert parse_date("15-Aug-2025") == date(2025, 8, 15)
        assert parse_date("29-Feb-2024") == date(2024, 2, 29)

    # --------------------------------------------------------------------------
    # 2. Case-Insensitive Month Names
    # --------------------------------------------------------------------------

    @pytest.mark.parametrize(
        "date_str,expected",
        [
            ("01-dec-2026", date(2026, 12, 1)),
            ("01-DEC-2026", date(2026, 12, 1)),
            ("01-Dec-2026", date(2026, 12, 1)),
            ("01-dEc-2026", date(2026, 12, 1)),
            ("15-jan-2025", date(2025, 1, 15)),
            ("15-JAN-2025", date(2025, 1, 15)),
            ("28-feb-2025", date(2025, 2, 28)),
            ("31-mar-2025", date(2025, 3, 31)),
            ("30-apr-2025", date(2025, 4, 30)),
            ("31-may-2025", date(2025, 5, 31)),
            ("30-jun-2025", date(2025, 6, 30)),
            ("31-jul-2025", date(2025, 7, 31)),
            ("31-aug-2025", date(2025, 8, 31)),
            ("30-sep-2025", date(2025, 9, 30)),
            ("31-oct-2025", date(2025, 10, 31)),
            ("30-nov-2025", date(2025, 11, 30)),
        ],
    )
    def test_case_insensitive_months(self, date_str: str, expected: date):
        assert parse_date(date_str) == expected

    # --------------------------------------------------------------------------
    # 3. Surrounding Whitespace Handling
    # --------------------------------------------------------------------------

    @pytest.mark.parametrize(
        "padded_date_str,expected",
        [
            ("  2026-12-01  ", date(2026, 12, 1)),
            ("\t01/12/2026\n", date(2026, 12, 1)),
            ("   01-12-2026   ", date(2026, 12, 1)),
            ("  01-Dec-2026  ", date(2026, 12, 1)),
        ],
    )
    def test_whitespace_trimming(self, padded_date_str: str, expected: date):
        assert parse_date(padded_date_str) == expected

    # --------------------------------------------------------------------------
    # 4. Strict Day-First Verification
    # --------------------------------------------------------------------------

    def test_strict_day_first_disambiguation(self):
        # 05/06/2026 must be parsed as 5th June 2026, NEVER May 6th
        assert parse_date("05/06/2026") == date(2026, 6, 5)
        assert parse_date("05-06-2026") == date(2026, 6, 5)

        # 07/08/2025 must be parsed as 7th August 2025
        assert parse_date("07/08/2025") == date(2025, 8, 7)

    # --------------------------------------------------------------------------
    # 5. Leap Year Handling
    # --------------------------------------------------------------------------

    def test_valid_leap_years(self):
        assert parse_date("29/02/2024") == date(2024, 2, 29)
        assert parse_date("29-02-2024") == date(2024, 2, 29)
        assert parse_date("29-Feb-2024") == date(2024, 2, 29)
        assert parse_date("2024-02-29") == date(2024, 2, 29)
        assert parse_date("29/02/2028") == date(2028, 2, 29)
        assert parse_date("29/02/2000") == date(2000, 2, 29)  # Century leap year

    @pytest.mark.parametrize(
        "invalid_leap_date",
        [
            "29/02/2025",
            "29-02-2025",
            "29-Feb-2025",
            "2025-02-29",
            "29/02/2026",
            "29/02/1900",  # 1900 was not a leap year
            "29/02/2100",  # 2100 is not a leap year
        ],
    )
    def test_invalid_leap_years_rejected(self, invalid_leap_date: str):
        with pytest.raises(InvalidDateError):
            parse_date(invalid_leap_date)

    # --------------------------------------------------------------------------
    # 6. Impossible Calendar Dates
    # --------------------------------------------------------------------------

    @pytest.mark.parametrize(
        "impossible_date",
        [
            "31/02/2026",      # Feb never has 31 days
            "30/02/2026",      # Feb never has 30 days
            "31/04/2026",      # April has 30 days
            "31-06-2026",      # June has 30 days
            "31-09-2026",      # September has 30 days
            "31-11-2026",      # November has 30 days
            "32-12-2026",      # Day 32 impossible
            "00-12-2026",      # Day 0 impossible
            "2026-02-31",      # ISO format impossible Feb 31
            "2026-04-31",      # ISO format impossible April 31
        ],
    )
    def test_impossible_calendar_dates_rejected(self, impossible_date: str):
        with pytest.raises(InvalidDateError):
            parse_date(impossible_date)

    # --------------------------------------------------------------------------
    # 7. Invalid Months
    # --------------------------------------------------------------------------

    @pytest.mark.parametrize(
        "invalid_month_date",
        [
            "2026-13-01",      # Month 13 in ISO
            "2026-00-01",      # Month 0 in ISO
            "01/13/2026",      # Month 13 in DD/MM/YYYY
            "01/00/2026",      # Month 0 in DD/MM/YYYY
            "01-Foo-2026",     # Invalid month abbreviation
            "15-Xyz-2025",     # Invalid month abbreviation
            "10-Bad-2024",     # Invalid month abbreviation
        ],
    )
    def test_invalid_months_rejected(self, invalid_month_date: str):
        with pytest.raises(InvalidDateError):
            parse_date(invalid_month_date)

    # --------------------------------------------------------------------------
    # 8. Unsupported Formats
    # --------------------------------------------------------------------------

    @pytest.mark.parametrize(
        "unsupported_format",
        [
            "12/31/2026",      # MM/DD/YYYY format (day 12, month 31 is invalid)
            "2026/12/01",      # Slashes with year first
            "2026.12.01",      # Dot separator
            "01.12.2026",      # Dot separator
            "Dec-01-2026",     # Month first text
            "2026-Dec-01",     # Year first text
            "December 1, 2026",# Long month name
            "01 Dec 2026",     # Space separated
            "20261201",        # Unseparated
            "NOT_A_DATE",      # Random text
        ],
    )
    def test_unsupported_formats_rejected(self, unsupported_format: str):
        with pytest.raises(InvalidDateError):
            parse_date(unsupported_format)

    # --------------------------------------------------------------------------
    # 9. Empty, Whitespace and None Inputs
    # --------------------------------------------------------------------------

    @pytest.mark.parametrize(
        "empty_input",
        [
            "",
            "   ",
            "\t\n",
        ],
    )
    def test_empty_input_rejected(self, empty_input: str):
        with pytest.raises(InvalidDateError) as exc_info:
            parse_date(empty_input)
        assert "empty or whitespace" in str(exc_info.value)

    def test_none_input_rejected(self):
        with pytest.raises(InvalidDateError) as exc_info:
            parse_date(None)
        assert "missing" in str(exc_info.value)

    def test_non_string_input_rejected(self):
        with pytest.raises(InvalidDateError) as exc_info:
            parse_date(20261201)  # type: ignore
        assert "must be a string" in str(exc_info.value)
