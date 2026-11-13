"""
Schema-order validation and rejection engine for lead ingestion.
Strictly adheres to Agentic Universe lead ingestion specifications.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Mapping

from src.ingestion.headers import HeaderMapping, build_header_mapping
from src.ingestion.normalizers import (
    NormalizationError,
    normalize_decimal,
    normalize_enum,
    normalize_indian_phone,
    parse_date,
)
from src.ingestion.row_reader import CSVRow, read_csv_rows


class SchemaValidationError(Exception):
    """Base exception for schema loading or validation configuration errors."""
    pass


@dataclass(frozen=True)
class AcceptedLeadRow:
    """Represents a lead row that successfully passed schema validation."""
    row_number: int
    values: dict[str, Any]
    warning_count: int


@dataclass(frozen=True)
class RejectedLeadRow:
    """Represents a lead row rejected during schema validation."""
    row_number: int
    reason: str


@dataclass
class ValidationBatchResult:
    """Aggregated validation results for a collection of CSV data rows."""
    accepted_rows: list[AcceptedLeadRow] = field(default_factory=list)
    rejected_rows: list[RejectedLeadRow] = field(default_factory=list)
    total_nonblank_rows: int = 0
    total_warnings: int = 0
    rejections_by_reason: dict[str, int] = field(default_factory=dict)

    @property
    def accepted_count(self) -> int:
        return len(self.accepted_rows)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected_rows)


def load_workflow_schema(schema_source: str | Path | dict[str, Any]) -> dict[str, Any]:
    """
    Loads and returns a workflow schema dictionary.

    Args:
        schema_source: Workflow ID (e.g. 'motor_renewal'), Path to JSON file, or dict.

    Returns:
        The validated workflow schema dictionary containing 'workflow_id' and 'fields'.
    """
    if isinstance(schema_source, dict):
        schema = schema_source
    elif isinstance(schema_source, (str, Path)):
        p = Path(schema_source)
        if not p.is_file():
            # Check relative to project schemas directory
            project_schema = Path(__file__).parent.parent.parent / "schemas" / f"{schema_source}.json"
            if project_schema.is_file():
                p = project_schema
            elif (Path(__file__).parent.parent.parent / "schemas" / f"{p.name}").is_file():
                p = Path(__file__).parent.parent.parent / "schemas" / f"{p.name}"
            else:
                raise FileNotFoundError(f"Workflow schema file not found: {schema_source}")

        try:
            with open(p, "r", encoding="utf-8") as f:
                schema = json.load(f)
        except Exception as e:
            raise SchemaValidationError(f"Failed to read schema file '{p}': {e}") from e
    else:
        raise SchemaValidationError("schema_source must be a dict, Path, or string")

    if not isinstance(schema, dict) or "fields" not in schema:
        raise SchemaValidationError("Workflow schema must be a dictionary with a 'fields' list")

    return schema


def _normalize_field_value(field_def: dict[str, Any], raw_value: str) -> Any:
    """
    Dispatches a raw cell value to the appropriate normalizer based on field type.

    Raises:
        NormalizationError (or subclasses) if validation fails.
    """
    field_type = field_def.get("type", "string").lower()

    if field_type == "string":
        return raw_value

    if field_type == "phone":
        return normalize_indian_phone(raw_value)

    if field_type == "date":
        return parse_date(raw_value)

    if field_type == "decimal":
        return normalize_decimal(raw_value)

    if field_type == "enum":
        allowed_values = field_def.get("values", [])
        return normalize_enum(raw_value, allowed_values)

    # Fallback for unrecognized types: keep trimmed string
    return raw_value


def validate_lead_row(
    row: CSVRow,
    header_mapping: HeaderMapping,
    schema_fields: list[dict[str, Any]],
) -> AcceptedLeadRow | RejectedLeadRow:
    """
    Validates a single non-blank CSVRow against schema fields in strict schema order.

    Rules:
    1. Walk fields in exact schema order.
    2. First failure terminates row validation immediately (single rejection reason).
    3. Missing required field -> 'missing_required:<field_name>'
    4. Invalid required field -> 'invalid_<field_type>' ('invalid_phone' for phone)
    5. Invalid optional field -> stored as None, increments warning_count, row continues.
    6. Missing optional field -> stored as None, zero warnings.

    Returns:
        AcceptedLeadRow if row passes, otherwise RejectedLeadRow.
    """
    parsed_values: dict[str, Any] = {}
    row_warnings = 0

    for field_def in schema_fields:
        field_name = field_def.get("name")
        if not field_name:
            continue

        field_type = field_def.get("type", "string").lower()
        is_required = field_def.get("required", False)

        # Extract value via mapped column index
        col_idx = header_mapping.get_index_by_field(field_name)
        raw_cell: str | None = None
        if col_idx is not None and col_idx < len(row.values):
            raw_cell = row.values[col_idx]

        # Empty string or None indicates missing value
        if raw_cell is None or raw_cell == "":
            if is_required:
                return RejectedLeadRow(
                    row_number=row.row_number,
                    reason=f"missing_required:{field_name}",
                )
            # Optional field missing: store None, no warning
            parsed_values[field_name] = None
            continue

        # Field is present: validate and normalize by type
        try:
            normalized_value = _normalize_field_value(field_def, raw_cell)
            parsed_values[field_name] = normalized_value
        except NormalizationError:
            if is_required:
                # Required field failed type validation
                rejection_reason = "invalid_phone" if field_type == "phone" else f"invalid_{field_type}"
                return RejectedLeadRow(
                    row_number=row.row_number,
                    reason=rejection_reason,
                )
            # Optional field failed type validation: store None and count one warning
            parsed_values[field_name] = None
            row_warnings += 1

    return AcceptedLeadRow(
        row_number=row.row_number,
        values=parsed_values,
        warning_count=row_warnings,
    )


def validate_lead_rows(
    rows: list[CSVRow],
    header_mapping: HeaderMapping,
    schema: dict[str, Any] | list[dict[str, Any]],
) -> ValidationBatchResult:
    """
    Validates a list of non-blank CSV rows against workflow schema fields.

    Calculates aggregated telemetry metrics:
    - Accepted rows
    - Rejected rows
    - Rejection reasons count
    - Total warnings (only counted for accepted rows)
    """
    if isinstance(schema, dict):
        schema_fields = schema.get("fields", [])
    elif isinstance(schema, list):
        schema_fields = schema
    else:
        raise SchemaValidationError("schema must be a dict with 'fields' or a list of field definitions")

    result = ValidationBatchResult(total_nonblank_rows=len(rows))

    for row in rows:
        row_result = validate_lead_row(row, header_mapping, schema_fields)
        if isinstance(row_result, AcceptedLeadRow):
            result.accepted_rows.append(row_result)
            result.total_warnings += row_result.warning_count
        elif isinstance(row_result, RejectedLeadRow):
            result.rejected_rows.append(row_result)
            reason = row_result.reason
            result.rejections_by_reason[reason] = result.rejections_by_reason.get(reason, 0) + 1

    return result


def validate_lead_file(
    file_path: str | Path,
    schema_source: str | Path | dict[str, Any],
) -> tuple[HeaderMapping, ValidationBatchResult]:
    """
    Full pipeline function for reading and validating a lead CSV file against a schema.

    Args:
        file_path: Path to the lead CSV file.
        schema_source: Workflow schema dict, workflow ID, or Path to schema JSON.

    Returns:
        tuple[HeaderMapping, ValidationBatchResult]
    """
    schema = load_workflow_schema(schema_source)
    headers, rows = read_csv_rows(file_path)
    header_mapping = build_header_mapping(headers, schema)
    validation_result = validate_lead_rows(rows, header_mapping, schema)
    return header_mapping, validation_result
