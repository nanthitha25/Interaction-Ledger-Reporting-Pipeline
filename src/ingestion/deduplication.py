"""
Lead deduplication and merging engine.
Strictly adheres to Agentic Universe lead ingestion specifications.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.ingestion.validator import AcceptedLeadRow


@dataclass(frozen=True)
class LeadIdentity:
    """
    Unique identity for a customer lead within a workflow.
    Defined as the composite tuple: (workflow_id, canonical_phone).
    """
    workflow_id: str
    canonical_phone: str


@dataclass
class LeadRecord:
    """
    Canonical persisted representation of a merged lead record.

    Attributes:
        identity: Unique (workflow_id, canonical_phone) identity.
        batch_id: Permanent file stem of the first valid occurrence (immutable).
        values: Dictionary mapping schema field names to current normalized values.
        row_count: Total number of valid rows merged into this lead.
    """
    identity: LeadIdentity
    batch_id: str
    values: dict[str, Any]
    row_count: int = 1

    @property
    def workflow_id(self) -> str:
        return self.identity.workflow_id

    @property
    def phone(self) -> str:
        return self.identity.canonical_phone


@dataclass
class DeduplicationResult:
    """Aggregated output of the deduplication engine."""
    leads: dict[LeadIdentity, LeadRecord] = field(default_factory=dict)
    duplicate_count: int = 0

    @property
    def total_leads(self) -> int:
        return len(self.leads)

    def get_leads_list(self) -> list[LeadRecord]:
        return list(self.leads.values())


def merge_lead_values(existing_values: dict[str, Any], incoming_values: dict[str, Any]) -> dict[str, Any]:
    """
    Merges incoming field values over existing field values using non-null last-write-wins semantics.

    Rules:
    - If incoming value is non-null (not None), it overwrites the existing value.
    - If incoming value is null (None), the existing value is preserved.
    """
    merged = dict(existing_values)
    for field_name, new_val in incoming_values.items():
        if new_val is not None:
            merged[field_name] = new_val
    return merged


def apply_schema_defaults(
    lead_values: dict[str, Any],
    schema_fields: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Applies schema default values to fields that remain None after all duplicates have merged.
    """
    result = dict(lead_values)
    for f in schema_fields:
        field_name = f.get("name")
        if not field_name:
            continue
        default_val = f.get("default")
        if default_val is not None and result.get(field_name) is None:
            result[field_name] = default_val
    return result


class LeadDeduplicationEngine:
    """
    Stateful in-memory lead deduplication and merging engine.

    Guarantees:
    - Identity: (workflow_id, canonical_phone).
    - Permanent batch_id locked on first valid occurrence.
    - Non-null last-write-wins merging for subsequent occurrences.
    - Defaults applied post-merge.
    """

    def __init__(self, schema_fields: list[dict[str, Any]] | None = None):
        self._leads: dict[LeadIdentity, LeadRecord] = {}
        self._duplicate_count: int = 0
        self._schema_fields: list[dict[str, Any]] = schema_fields or []

    @property
    def leads(self) -> dict[LeadIdentity, LeadRecord]:
        return self._leads

    @property
    def duplicate_count(self) -> int:
        return self._duplicate_count

    def process_accepted_row(
        self,
        workflow_id: str,
        batch_id: str,
        row: AcceptedLeadRow,
    ) -> tuple[LeadRecord, bool]:
        """
        Processes a single AcceptedLeadRow.

        Args:
            workflow_id: Workflow identifier (e.g. 'motor_renewal').
            batch_id: File stem of the current batch (e.g. 'batch_001').
            row: Validated AcceptedLeadRow.

        Returns:
            tuple[LeadRecord, bool]: (lead_record, is_new_lead)
        """
        canonical_phone = row.values.get("phone")
        if not canonical_phone or not isinstance(canonical_phone, str):
            raise ValueError(f"Accepted row {row.row_number} is missing canonical phone")

        identity = LeadIdentity(workflow_id=workflow_id, canonical_phone=canonical_phone)

        if identity not in self._leads:
            # First occurrence: creates the lead and locks permanent batch_id
            record = LeadRecord(
                identity=identity,
                batch_id=batch_id,
                values=dict(row.values),
                row_count=1,
            )
            self._leads[identity] = record
            return record, True

        # Subsequent occurrence: duplicate merge
        self._duplicate_count += 1
        record = self._leads[identity]
        record.row_count += 1

        # Last-write-wins (non-null) update
        record.values = merge_lead_values(record.values, row.values)
        # Permanent batch_id remains strictly unchanged
        return record, False

    def apply_defaults(self, schema_fields: list[dict[str, Any]] | None = None) -> None:
        """
        Applies schema default values to fields that remain None across all merged leads.
        Must only be called after all duplicates have been merged.
        """
        fields = schema_fields or self._schema_fields
        if not fields:
            return

        for record in self._leads.values():
            record.values = apply_schema_defaults(record.values, fields)

    def get_result(self) -> DeduplicationResult:
        """Returns the final deduplication result snapshot."""
        return DeduplicationResult(
            leads=dict(self._leads),
            duplicate_count=self._duplicate_count,
        )


def deduplicate_lead_batches(
    batches: list[tuple[str, str, list[AcceptedLeadRow]]],
    schema_fields: list[dict[str, Any]] | None = None,
    apply_defaults_at_end: bool = True,
) -> DeduplicationResult:
    """
    Convenience orchestrator for processing multiple batches in strict deterministic order.

    Args:
        batches: Ordered list of (workflow_id, batch_id, accepted_rows).
        schema_fields: Optional schema fields definition for applying defaults post-merge.
        apply_defaults_at_end: Whether to apply schema defaults after all rows are merged.

    Returns:
        DeduplicationResult containing merged LeadRecord objects and duplicate counts.
    """
    engine = LeadDeduplicationEngine(schema_fields=schema_fields)

    # Process batches in strict list order (respecting drop, file, and row order)
    for workflow_id, batch_id, rows in batches:
        for row in rows:
            engine.process_accepted_row(workflow_id, batch_id, row)

    if apply_defaults_at_end and schema_fields:
        engine.apply_defaults()

    return engine.get_result()
