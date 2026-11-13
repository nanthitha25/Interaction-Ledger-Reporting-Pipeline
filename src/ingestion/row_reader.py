"""
CSV row reader and blank row filter module.
Strictly adheres to Agentic Universe lead ingestion specifications.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO


class RowReaderError(Exception):
    """Base exception for CSV row reader errors."""
    pass


@dataclass(frozen=True)
class CSVRow:
    """
    Represents a single non-blank data row in a CSV file.

    Attributes:
        row_number: 1-based index counting only non-blank data rows.
        values: List of trimmed string values for each column.
    """
    row_number: int
    values: list[str]


def _clean_cell(cell: str) -> str:
    """Strips leading/trailing whitespace and lingering UTF-8 BOM characters."""
    return cell.strip().lstrip("\ufeff").strip()


def parse_csv_stream(stream: TextIO) -> tuple[list[str], list[CSVRow]]:
    """
    Parses a CSV text stream, extracts headers, filters blank rows, trims cell values,
    and assigns 1-based sequential row numbers to non-blank data rows.

    Args:
        stream: An open readable text stream (e.g., from open() or io.StringIO).

    Returns:
        A tuple of (headers, data_rows):
        - headers: List of trimmed column header strings (empty list if no header found).
        - data_rows: List of CSVRow objects.
    """
    reader = csv.reader(stream)

    # 1. Locate the header row (the first non-blank row in the stream)
    raw_header: list[str] | None = None
    for row in reader:
        # Check if row has at least one non-whitespace cell
        if any(_clean_cell(cell) for cell in row):
            raw_header = row
            break

    # If stream was empty or contained only blank rows
    if raw_header is None:
        return [], []

    # Headers are trimmed of surrounding whitespace and BOM
    headers = [_clean_cell(cell) for cell in raw_header]

    # 2. Iterate through remaining rows, skip blank rows, and maintain 1-based row counter
    data_rows: list[CSVRow] = []
    row_number = 0

    for raw_row in reader:
        # A row is blank if every field is empty or contains only whitespace
        if not any(_clean_cell(cell) for cell in raw_row):
            continue

        row_number += 1
        # Trim leading and trailing whitespace from every field
        trimmed_values = [_clean_cell(cell) for cell in raw_row]
        data_rows.append(CSVRow(row_number=row_number, values=trimmed_values))

    return headers, data_rows


def read_csv_rows(file_path: str | Path) -> tuple[list[str], list[CSVRow]]:
    """
    Reads a CSV file from disk, handles UTF-8 and UTF-8 BOM encodings transparently,
    and returns the headers and non-blank data rows.

    Args:
        file_path: Path to the CSV file on disk.

    Returns:
        tuple[list[str], list[CSVRow]] containing headers and trimmed non-blank data rows.

    Raises:
        FileNotFoundError: If the specified file does not exist.
        RowReaderError: If reading or decoding the file encounters an error.
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"CSV file not found: {path}")

    try:
        # 'utf-8-sig' transparently strips the UTF-8 BOM (\ufeff) if present
        with open(path, mode="r", encoding="utf-8-sig", newline="") as f:
            return parse_csv_stream(f)
    except UnicodeDecodeError as e:
        raise RowReaderError(f"Failed to decode CSV file '{path}' using UTF-8: {e}") from e
    except Exception as e:
        if isinstance(e, (FileNotFoundError, RowReaderError)):
            raise
        raise RowReaderError(f"Error reading CSV file '{path}': {e}") from e
