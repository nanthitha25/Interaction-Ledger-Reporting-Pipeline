"""
Header normalization and schema alias matching module.
Strictly adheres to Agentic Universe lead ingestion specifications.
"""
from __future__ import annotations

import re
from typing import Any, Mapping


class HeaderMappingError(ValueError):
    """Base exception for header mapping errors."""
    pass


class AmbiguousHeaderError(HeaderMappingError):
    """Raised when header mapping is ambiguous or conflicting."""
    pass


def normalize_header(header: str | None) -> str:
    """
    Normalizes a CSV header or schema alias string.

    Rules:
    1. Lowercase the string.
    2. Replace every run of underscores ('_') or whitespace characters with a single space (' ').
    3. Trim leading and trailing whitespace.

    Examples:
    - "Customer Name" -> "customer name"
    - "customer_name" -> "customer name"
    - "CUSTOMER_NAME" -> "customer name"
    - "  _ _ CUSTOMER   _  NAME _ _ " -> "customer name"
    - None or whitespace only -> ""
    """
    if header is None:
        return ""

    if not isinstance(header, str):
        header = str(header)

    # Convert to lowercase
    s = header.lower()

    # Replace every run of underscores or whitespace with a single ASCII space
    s = re.sub(r"[\s_]+", " ", s)

    # Trim leading and trailing whitespace
    return s.strip()


class HeaderMapping(dict):
    """
    Result of mapping CSV headers to schema fields.

    Inherits from dict where keys are column indices (int) and values are
    canonical schema field names (str).

    Also supports lookup by raw header name or normalized header name for flexibility.
    """

    def __init__(
        self,
        by_index: dict[int, str],
        by_raw_header: dict[str, str],
        by_field: dict[str, int],
        unmapped_indices: list[int],
        unmapped_headers: list[str],
        csv_headers: list[str],
    ):
        super().__init__(by_index)
        self.by_index = by_index
        self.by_raw_header = by_raw_header
        self.by_field = by_field
        self.unmapped_indices = unmapped_indices
        self.unmapped_headers = unmapped_headers
        self.csv_headers = csv_headers

    def __getitem__(self, key: Any) -> str:
        if isinstance(key, int):
            return self.by_index[key]
        if isinstance(key, str):
            if key in self.by_raw_header:
                return self.by_raw_header[key]
            # Try normalized lookup
            norm = normalize_header(key)
            for raw, target in self.by_raw_header.items():
                if normalize_header(raw) == norm:
                    return target
        raise KeyError(f"Header or column index not found in mapping: {key!r}")

    def __contains__(self, key: Any) -> bool:
        if isinstance(key, int):
            return key in self.by_index
        if isinstance(key, str):
            if key in self.by_raw_header:
                return True
            norm = normalize_header(key)
            return any(normalize_header(raw) == norm for raw in self.by_raw_header)
        return False

    def get_field_by_index(self, col_idx: int) -> str | None:
        """Returns canonical schema field name for column index, or None if unmapped."""
        return self.by_index.get(col_idx)

    def get_index_by_field(self, field_name: str) -> int | None:
        """Returns column index for a canonical schema field name, or None if missing."""
        return self.by_field.get(field_name)

    @property
    def mapped_fields(self) -> set[str]:
        """Returns set of all schema field names successfully mapped from the CSV."""
        return set(self.by_field.keys())

    def get_missing_required_fields(self, schema_fields: list[dict[str, Any]]) -> list[str]:
        """
        Returns list of required schema field names that are not present in this header mapping.
        """
        missing = []
        for f in schema_fields:
            if f.get("required", False) and f["name"] not in self.by_field:
                missing.append(f["name"])
        return missing


def build_header_mapping(
    csv_headers: list[str],
    workflow_schema: dict[str, Any] | list[dict[str, Any]],
) -> HeaderMapping:
    """
    Builds a deterministic mapping from CSV column headers to canonical workflow schema fields.

    Args:
        csv_headers: List of raw header strings from the CSV header row.
        workflow_schema: Workflow schema dict containing 'fields' list, or the fields list itself.

    Returns:
        HeaderMapping object (accessible by column index or header name).

    Raises:
        AmbiguousHeaderError: If multiple CSV columns map to the same schema field,
                              or if schema definitions contain conflicting aliases.
        HeaderMappingError: If input arguments are invalid.
    """
    if not isinstance(csv_headers, (list, tuple)):
        raise HeaderMappingError("csv_headers must be a list or tuple of strings")

    # Extract fields list from schema
    if isinstance(workflow_schema, dict):
        if "fields" not in workflow_schema:
            raise HeaderMappingError("workflow_schema dict must contain 'fields' key")
        schema_fields = workflow_schema["fields"]
    elif isinstance(workflow_schema, list):
        schema_fields = workflow_schema
    else:
        raise HeaderMappingError("workflow_schema must be a dict or list of field definitions")

    # 1. Build lookup dictionary from normalized candidate string -> canonical field name
    # Candidate strings include normalized field name and normalized aliases.
    alias_to_field: dict[str, str] = {}
    for field_def in schema_fields:
        field_name = field_def.get("name")
        if not field_name:
            continue

        norm_name = normalize_header(field_name)
        if norm_name in alias_to_field and alias_to_field[norm_name] != field_name:
            raise AmbiguousHeaderError(
                f"Schema definition conflict: field name '{field_name}' conflicts with alias for '{alias_to_field[norm_name]}'"
            )
        alias_to_field[norm_name] = field_name

        for alias in field_def.get("aliases", []):
            norm_alias = normalize_header(alias)
            if not norm_alias:
                continue
            if norm_alias in alias_to_field and alias_to_field[norm_alias] != field_name:
                raise AmbiguousHeaderError(
                    f"Schema definition conflict: alias '{alias}' (normalized: '{norm_alias}') "
                    f"is mapped to both '{alias_to_field[norm_alias]}' and '{field_name}'"
                )
            alias_to_field[norm_alias] = field_name

    # 2. Iterate through CSV headers and map to schema fields
    by_index: dict[int, str] = {}
    by_raw_header: dict[str, str] = {}
    by_field: dict[str, int] = {}
    unmapped_indices: list[int] = []
    unmapped_headers: list[str] = []

    for col_idx, raw_header in enumerate(csv_headers):
        norm_h = normalize_header(raw_header)

        if norm_h in alias_to_field:
            target_field = alias_to_field[norm_h]

            # Ambiguity check: has this schema field already been mapped to an earlier column?
            if target_field in by_field:
                prev_idx = by_field[target_field]
                prev_raw = csv_headers[prev_idx]
                raise AmbiguousHeaderError(
                    f"Ambiguous CSV headers: both column {prev_idx} ('{prev_raw}') and "
                    f"column {col_idx} ('{raw_header}') map to schema field '{target_field}'"
                )

            by_index[col_idx] = target_field
            by_raw_header[raw_header] = target_field
            by_field[target_field] = col_idx
        else:
            # Unmapped column (safely ignored per spec)
            unmapped_indices.append(col_idx)
            unmapped_headers.append(raw_header)

    return HeaderMapping(
        by_index=by_index,
        by_raw_header=by_raw_header,
        by_field=by_field,
        unmapped_indices=unmapped_indices,
        unmapped_headers=unmapped_headers,
        csv_headers=list(csv_headers),
    )
