"""
Unit tests for CSV row reader and blank row filter (Task LEAD-05).
Validates rules specified in FR-LD-003, FR-LD-004, and BR-002.
"""
from pathlib import Path
import pytest

from src.ingestion.row_reader import CSVRow, RowReaderError, read_csv_rows


class TestCSVRowReader:
    """Test suite for read_csv_rows()."""

    def test_1_normal_csv_with_multiple_rows(self, tmp_path: Path):
        content = (
            "name,phone,premium\n"
            "John,9876543210,15000\n"
            "Jane,9123456789,20000\n"
            "Alex,8765432109,25000\n"
        )
        csv_file = tmp_path / "normal.csv"
        csv_file.write_text(content, encoding="utf-8")

        headers, rows = read_csv_rows(csv_file)

        assert headers == ["name", "phone", "premium"]
        assert len(rows) == 3

        assert rows[0] == CSVRow(row_number=1, values=["John", "9876543210", "15000"])
        assert rows[1] == CSVRow(row_number=2, values=["Jane", "9123456789", "20000"])
        assert rows[2] == CSVRow(row_number=3, values=["Alex", "8765432109", "25000"])

    def test_2_empty_csv(self, tmp_path: Path):
        csv_file = tmp_path / "empty.csv"
        csv_file.write_text("", encoding="utf-8")

        headers, rows = read_csv_rows(csv_file)
        assert headers == []
        assert rows == []

    def test_3_header_only_csv(self, tmp_path: Path):
        csv_file = tmp_path / "header_only.csv"
        csv_file.write_text("name,phone,premium\n", encoding="utf-8")

        headers, rows = read_csv_rows(csv_file)
        assert headers == ["name", "phone", "premium"]
        assert rows == []

    def test_4_completely_blank_rows(self, tmp_path: Path):
        content = (
            "name,phone,premium\n"
            "\n"
            "John,9876543210,15000\n"
            "\n"
            "\n"
            "Jane,9123456789,20000\n"
            "\n"
        )
        csv_file = tmp_path / "blank_rows.csv"
        csv_file.write_text(content, encoding="utf-8")

        headers, rows = read_csv_rows(csv_file)
        assert headers == ["name", "phone", "premium"]
        assert len(rows) == 2
        assert rows[0] == CSVRow(row_number=1, values=["John", "9876543210", "15000"])
        assert rows[1] == CSVRow(row_number=2, values=["Jane", "9123456789", "20000"])

    def test_5_rows_containing_whitespace_only(self, tmp_path: Path):
        content = (
            "name,phone,premium\n"
            "   ,   ,   \n"
            "John,9876543210,15000\n"
            " \t , \n"
            "   \n"
            "Jane,9123456789,20000\n"
        )
        csv_file = tmp_path / "whitespace_rows.csv"
        csv_file.write_text(content, encoding="utf-8")

        headers, rows = read_csv_rows(csv_file)
        assert headers == ["name", "phone", "premium"]
        assert len(rows) == 2
        assert rows[0] == CSVRow(row_number=1, values=["John", "9876543210", "15000"])
        assert rows[1] == CSVRow(row_number=2, values=["Jane", "9123456789", "20000"])

    def test_6_leading_and_trailing_whitespace_trimmed(self, tmp_path: Path):
        content = (
            "  name  ,  phone  ,  premium  \n"
            "  John  ,  9876543210 ,  15000  \n"
            "  Jane  ,  9123456789  ,  20000 \n"
        )
        csv_file = tmp_path / "trimmed.csv"
        csv_file.write_text(content, encoding="utf-8")

        headers, rows = read_csv_rows(csv_file)
        assert headers == ["name", "phone", "premium"]
        assert rows[0].values == ["John", "9876543210", "15000"]
        assert rows[1].values == ["Jane", "9123456789", "20000"]

    def test_7_rows_containing_some_empty_fields(self, tmp_path: Path):
        # A row with at least one non-whitespace value must be retained
        content = (
            "name,phone,premium\n"
            "John,,15000\n"
            ",9123456789,\n"
            ",,\n"  # Completely empty - must be skipped
            "Alex,8765432109,\n"
        )
        csv_file = tmp_path / "partial_empty.csv"
        csv_file.write_text(content, encoding="utf-8")

        headers, rows = read_csv_rows(csv_file)
        assert len(rows) == 3
        assert rows[0] == CSVRow(row_number=1, values=["John", "", "15000"])
        assert rows[1] == CSVRow(row_number=2, values=["", "9123456789", ""])
        assert rows[2] == CSVRow(row_number=3, values=["Alex", "8765432109", ""])

    def test_8_exact_nonblank_row_numbering(self, tmp_path: Path):
        # Matches the assignment specification example table
        content = (
            "name,phone,premium\n"
            "John,9876543210,15000\n"     # Row 1
            "   \n"                         # Skipped
            "Jane,9123456789,20000\n"     # Row 2
            "Alex,8765432109,25000\n"     # Row 3
        )
        csv_file = tmp_path / "numbering.csv"
        csv_file.write_text(content, encoding="utf-8")

        headers, rows = read_csv_rows(csv_file)
        assert [r.row_number for r in rows] == [1, 2, 3]
        assert [r.values[0] for r in rows] == ["John", "Jane", "Alex"]

    def test_9_utf8_bom_handling(self, tmp_path: Path):
        # Test binary UTF-8 BOM (\xef\xbb\xbf)
        bom_bytes = b"\xef\xbb\xbfname,phone,premium\nJohn,9876543210,15000\n"
        csv_file = tmp_path / "bom_bytes.csv"
        csv_file.write_bytes(bom_bytes)

        headers, rows = read_csv_rows(csv_file)
        # BOM must not contaminate the first header column
        assert headers[0] == "name"
        assert headers == ["name", "phone", "premium"]
        assert len(rows) == 1
        assert rows[0].values == ["John", "9876543210", "15000"]

    def test_10_csv_values_containing_quoted_commas(self, tmp_path: Path):
        content = (
            'name,phone,address\n'
            '"Sharma, Aarav",9876543210,"Flat 101, Palm Beach, Mumbai"\n'
            '"Iyer, Kavya",9123456789,"Indiranagar, Bangalore"\n'
        )
        csv_file = tmp_path / "quoted_commas.csv"
        csv_file.write_text(content, encoding="utf-8")

        headers, rows = read_csv_rows(csv_file)
        assert headers == ["name", "phone", "address"]
        assert len(rows) == 2
        assert rows[0].values == ["Sharma, Aarav", "9876543210", "Flat 101, Palm Beach, Mumbai"]
        assert rows[1].values == ["Iyer, Kavya", "9123456789", "Indiranagar, Bangalore"]

    def test_11_quoted_values_containing_newlines(self, tmp_path: Path):
        content = (
            'name,phone,notes\n'
            '"John",9876543210,"Line 1\nLine 2"\n'
            '"Jane",9123456789,"Single line note"\n'
        )
        csv_file = tmp_path / "quoted_newlines.csv"
        csv_file.write_text(content, encoding="utf-8")

        headers, rows = read_csv_rows(csv_file)
        assert len(rows) == 2
        assert rows[0].row_number == 1
        assert rows[0].values == ["John", "9876543210", "Line 1\nLine 2"]
        assert rows[1].row_number == 2
        assert rows[1].values == ["Jane", "9123456789", "Single line note"]

    def test_12_nonexistent_file_raises_filenotfound(self):
        with pytest.raises(FileNotFoundError):
            read_csv_rows("non_existent_file.csv")

    def test_13_file_starts_with_blank_lines_before_header(self, tmp_path: Path):
        content = (
            "\n"
            "   \n"
            "name,phone,premium\n"
            "John,9876543210,15000\n"
        )
        csv_file = tmp_path / "preceding_blanks.csv"
        csv_file.write_text(content, encoding="utf-8")

        headers, rows = read_csv_rows(csv_file)
        assert headers == ["name", "phone", "premium"]
        assert len(rows) == 1
        assert rows[0].row_number == 1
        assert rows[0].values == ["John", "9876543210", "15000"]
