"""
Normalizers and type parsers for lead ingestion and event processing.
Strictly adheres to Agentic Universe business rules.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from typing import Iterable


class NormalizationError(ValueError):
    """Base exception for data normalization errors."""
    pass


class InvalidPhoneError(NormalizationError):
    """Raised when a phone number fails Indian mobile phone normalization rules."""
    pass


class InvalidDateError(NormalizationError):
    """Raised when a date string fails day-first date validation rules."""
    pass


class InvalidDecimalError(NormalizationError):
    """Raised when a decimal value fails parsing or validation rules."""
    pass


class InvalidEnumError(NormalizationError):
    """Raised when an enum value fails case-insensitive matching."""
    pass


# Mapping for English month abbreviations (case-insensitive)
_MONTH_ABBREVIATIONS: dict[str, int] = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

# Compiled regular expressions for supported formats
_RE_YYYY_MM_DD = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_RE_DD_MM_YYYY_SLASH = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_RE_DD_MM_YYYY_DASH = re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$")
_RE_DD_MON_YYYY = re.compile(r"^(\d{1,2})-([a-zA-Z]{3})-(\d{4})$")

# Currency prefixes: ₹, Rs, Rs., INR (case-insensitive, optional trailing space)
_RE_CURRENCY_PREFIX = re.compile(r"^(?:₹|rs\.?|inr)\s*", re.IGNORECASE)


def normalize_indian_phone(raw_phone: str | None) -> str:
    """
    Normalizes an Indian mobile phone number to canonical E.164 format (+91XXXXXXXXXX).

    Rules:
    1. Strip all non-digit characters.
    2. If 12 digits starting with '91', drop '91'.
    3. If 11 digits starting with '0', drop '0'.
    4. The resulting digit string must be exactly 10 digits.
    5. The first digit must be 6, 7, 8, or 9.
    6. Return canonical format: '+91' + 10 digits.
    7. Otherwise, raise InvalidPhoneError.

    Note:
    To protect PII, exception messages do not include the raw or normalized phone number.
    """
    if raw_phone is None:
        raise InvalidPhoneError("Phone number is missing (None)")

    if not isinstance(raw_phone, str):
        raise InvalidPhoneError("Phone number must be a string")

    # Strip all non-digit characters
    digits = re.sub(r"\D", "", raw_phone)

    # If 12 digits starting with '91', drop '91'
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    # If 11 digits starting with '0', drop '0'
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    # Must be exactly 10 digits
    if len(digits) != 10:
        raise InvalidPhoneError(f"Invalid phone digit length: expected 10, got {len(digits)}")

    # First digit must be 6, 7, 8, or 9
    if digits[0] not in {"6", "7", "8", "9"}:
        raise InvalidPhoneError(f"Invalid Indian mobile prefix: expected 6, 7, 8, or 9, got starting digit '{digits[0]}'")

    return f"+91{digits}"


def parse_date(value: str | None) -> date:
    """
    Parses a date string using strict day-first semantics into a standard datetime.date object.

    Supported Formats (day-first):
    1. YYYY-MM-DD
    2. DD/MM/YYYY
    3. DD-MM-YYYY
    4. DD-Mon-YYYY (e.g. 01-Dec-2026, month case-insensitive)

    Validation:
    - Strips surrounding whitespace.
    - Rejects impossible calendar dates (e.g., 31/02/2026, 29/02/2025).
    - Rejects invalid months, invalid days, and unsupported formats.
    - Raises InvalidDateError on invalid inputs, empty strings, or None.
    """
    if value is None:
        raise InvalidDateError("Date value is missing (None)")

    if not isinstance(value, str):
        raise InvalidDateError("Date value must be a string")

    val = value.strip()
    if not val:
        raise InvalidDateError("Date string is empty or whitespace")

    # 1. Format: YYYY-MM-DD
    m_iso = _RE_YYYY_MM_DD.match(val)
    if m_iso:
        year, month, day = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        try:
            return date(year, month, day)
        except ValueError as e:
            raise InvalidDateError(f"Impossible calendar date in YYYY-MM-DD format: {e}") from e

    # 2. Format: DD/MM/YYYY (Day-first)
    m_slash = _RE_DD_MM_YYYY_SLASH.match(val)
    if m_slash:
        day, month, year = int(m_slash.group(1)), int(m_slash.group(2)), int(m_slash.group(3))
        try:
            return date(year, month, day)
        except ValueError as e:
            raise InvalidDateError(f"Impossible calendar date in DD/MM/YYYY format: {e}") from e

    # 3. Format: DD-MM-YYYY (Day-first)
    m_dash = _RE_DD_MM_YYYY_DASH.match(val)
    if m_dash:
        day, month, year = int(m_dash.group(1)), int(m_dash.group(2)), int(m_dash.group(3))
        try:
            return date(year, month, day)
        except ValueError as e:
            raise InvalidDateError(f"Impossible calendar date in DD-MM-YYYY format: {e}") from e

    # 4. Format: DD-Mon-YYYY (e.g. 01-Dec-2026, case-insensitive)
    m_mon = _RE_DD_MON_YYYY.match(val)
    if m_mon:
        day = int(m_mon.group(1))
        mon_str = m_mon.group(2).lower()
        year = int(m_mon.group(3))

        if mon_str not in _MONTH_ABBREVIATIONS:
            raise InvalidDateError(f"Invalid month abbreviation: '{mon_str}'")

        month = _MONTH_ABBREVIATIONS[mon_str]
        try:
            return date(year, month, day)
        except ValueError as e:
            raise InvalidDateError(f"Impossible calendar date in DD-Mon-YYYY format: {e}") from e

    # Any other format is unsupported
    raise InvalidDateError(f"Unsupported date format: '{val}' does not match any allowed format")


def normalize_decimal(value: str | None) -> Decimal:
    """
    Parses and normalizes a decimal currency value.

    Rules:
    1. Strips leading and trailing whitespace.
    2. Strips optional currency prefixes (case-insensitive): '₹', 'Rs', 'Rs.', 'INR'.
    3. Handles optional whitespace after the prefix.
    4. Removes commas used as thousands separators.
    5. Validates that the numeric value is >= 0.00.
    6. Uses Python Decimal and rounds to exactly 2 decimal places using half-up rounding.
    7. Raises InvalidDecimalError on negative amounts, malformed strings, or missing values.
    """
    if value is None:
        raise InvalidDecimalError("Decimal value is missing (None)")

    if not isinstance(value, str):
        raise InvalidDecimalError("Decimal value must be a string")

    val = value.strip()
    if not val:
        raise InvalidDecimalError("Decimal string is empty or whitespace")

    # Strip currency prefix (case-insensitive, optional trailing space)
    val = _RE_CURRENCY_PREFIX.sub("", val).strip()
    if not val:
        raise InvalidDecimalError("Decimal string contains only currency prefix without numeric amount")

    # Remove commas
    val = val.replace(",", "")

    try:
        d = Decimal(val)
    except InvalidOperation as e:
        raise InvalidDecimalError(f"Malformed decimal value: could not parse numeric amount") from e

    if d < Decimal("0"):
        raise InvalidDecimalError(f"Decimal value must be non-negative (>= 0)")

    # Round to exactly 2 decimal places
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def normalize_enum(value: str | None, allowed_values: Iterable[str]) -> str:
    """
    Normalizes and validates an enum string value against a schema-defined set of allowed values.

    Rules:
    1. Strips surrounding whitespace.
    2. Converts to lowercase.
    3. Validates against allowed_values case-insensitively.
    4. Returns canonical lowercase enum string.
    5. Raises InvalidEnumError if value is invalid, empty, or None.
    """
    if value is None:
        raise InvalidEnumError("Enum value is missing (None)")

    if not isinstance(value, str):
        raise InvalidEnumError("Enum value must be a string")

    val = value.strip().lower()
    if not val:
        raise InvalidEnumError("Enum value is empty or whitespace")

    # Normalize allowed values set to lowercase
    allowed_map = {
        str(allowed).strip().lower(): str(allowed).strip().lower()
        for allowed in allowed_values
        if str(allowed).strip()
    }

    if val in allowed_map:
        return allowed_map[val]

    raise InvalidEnumError(f"Invalid enum value; permitted values are: {sorted(allowed_map.keys())}")
