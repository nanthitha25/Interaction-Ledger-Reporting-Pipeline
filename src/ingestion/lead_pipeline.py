"""
Lead Ingestion Pipeline.

Connects CSV validation, schema mapping, within-drop deduplication,
and the PostgreSQL LeadRepository into an atomic, idempotent ingestion pipeline.
Follows specs/02-data-ingestion.md and specs/05-database-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import logging
from pathlib import Path
from typing import Any, Optional, Sequence

from psycopg2.extensions import connection as PgConnection

from src.db.connection import get_cursor, transaction
from src.ingestion.deduplication import LeadDeduplicationEngine, LeadRecord
from src.ingestion.validator import (
    AcceptedLeadRow,
    RejectedLeadRow,
    ValidationBatchResult,
    load_workflow_schema,
    validate_lead_file,
)
from src.repository.lead_repository import LeadModel, LeadRepository

logger = logging.getLogger(__name__)


class LeadPipelineError(Exception):
    """Base exception for lead pipeline operations."""
    pass


class FileIntegrityViolationError(LeadPipelineError):
    """Raised when a file's content hash does not match an existing checkpoint in the same drop."""
    pass


@dataclass
class LeadIngestionSummary:
    """
    Detailed telemetry and execution metrics for a processed lead CSV file.
    """
    drop_id: str
    workflow_id: str
    batch_id: str
    file_path: str
    file_hash: str
    status: str  # 'PROCESSED' or 'SKIPPED'
    total_nonblank_rows: int = 0
    accepted_rows: int = 0
    rejected_rows: int = 0
    duplicate_rows_merged: int = 0
    inserted_leads: int = 0
    updated_leads: int = 0
    warning_count: int = 0
    rejections_by_reason: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Returns summary metrics as a dictionary."""
        return {
            "drop_id": self.drop_id,
            "workflow_id": self.workflow_id,
            "batch_id": self.batch_id,
            "file_path": self.file_path,
            "file_hash": self.file_hash,
            "status": self.status,
            "total_nonblank_rows": self.total_nonblank_rows,
            "accepted_rows": self.accepted_rows,
            "rejected_rows": self.rejected_rows,
            "duplicate_rows_merged": self.duplicate_rows_merged,
            "inserted_leads": self.inserted_leads,
            "updated_leads": self.updated_leads,
            "warning_count": self.warning_count,
            "rejections_by_reason": dict(self.rejections_by_reason),
        }


def compute_file_sha256(file_path: str | Path) -> str:
    """Computes the lowercase hexadecimal SHA-256 hash of a file."""
    p = Path(file_path)
    sha256 = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(65536):
            sha256.update(chunk)
    return sha256.hexdigest()


def ingest_lead_csv(
    file_path: str | Path,
    drop_id: str,
    workflow_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    schema_source: Optional[str | Path | dict[str, Any]] = None,
    repository: Optional[LeadRepository] = None,
    conn: Optional[PgConnection] = None,
) -> LeadIngestionSummary:
    """
    Ingests a lead CSV file into PostgreSQL through schema validation,
    in-memory deduplication, and atomic database persistence.

    Enforces:
    - 6-scenario file idempotency (detects identical re-runs and hash mutations).
    - Schema validation and order-preserving rejections.
    - Intra-batch and cross-batch deduplication via LeadRepository.
    - Cryptographic PII protection at rest.

    Args:
        file_path: Path to the lead CSV file.
        drop_id: Current drop run identifier (e.g. 'drop_1').
        workflow_id: Optional workflow name. Inferred from parent directory if omitted.
        batch_id: Optional batch file stem. Inferred from file stem if omitted.
        schema_source: Optional schema dict, path, or name. Defaults to workflow_id.
        repository: Optional LeadRepository instance.
        conn: Optional active database connection for outer transaction sharing.

    Returns:
        LeadIngestionSummary containing detailed ingestion metrics.
    """
    path_obj = Path(file_path).resolve()
    if not path_obj.is_file():
        raise FileNotFoundError(f"Lead file not found: {file_path}")

    # Determine workflow_id and batch_id
    resolved_workflow = workflow_id or path_obj.parent.name
    resolved_batch = batch_id or path_obj.stem
    file_hash = compute_file_sha256(path_obj)
    rel_path = str(file_path)

    repo = repository or LeadRepository()

    def _process_in_transaction(connection: PgConnection) -> LeadIngestionSummary:
        with get_cursor(connection) as cur:
            # 1. Ensure ingestion_runs record exists for drop_id (FK requirement)
            cur.execute(
                """
                INSERT INTO ingestion_runs (drop_id, ingest_order, status)
                VALUES (%s, (SELECT COALESCE(MAX(ingest_order), 0) + 1 FROM ingestion_runs), 'IN_PROGRESS')
                ON CONFLICT (drop_id) DO NOTHING;
                """,
                (drop_id,),
            )

            # 2. Checkpoint check (Idempotency Scenario 1 & 2)
            cur.execute(
                """
                SELECT file_hash, status, records_count
                FROM ingestion_file_checkpoints
                WHERE drop_id = %s AND file_path = %s;
                """,
                (drop_id, rel_path),
            )
            existing_checkpoint = cur.fetchone()

            if existing_checkpoint:
                prior_hash, prior_status, prior_records = existing_checkpoint
                if prior_hash == file_hash:
                    # Scenario 1: Identical file re-ingestion -> cleanly skip
                    logger.info("File %s already processed in %s. Skipping.", rel_path, drop_id)
                    return LeadIngestionSummary(
                        drop_id=drop_id,
                        workflow_id=resolved_workflow,
                        batch_id=resolved_batch,
                        file_path=rel_path,
                        file_hash=file_hash,
                        status="SKIPPED",
                        total_nonblank_rows=prior_records,
                        accepted_rows=prior_records,
                    )
                else:
                    # Scenario 2: Same filename with changed content -> abort transaction
                    raise FileIntegrityViolationError(
                        f"File integrity violation for {rel_path} in {drop_id}: "
                        f"Hash has changed from {prior_hash} to {file_hash}."
                    )

            # 3. Load schema and validate CSV file
            schema = load_workflow_schema(schema_source or resolved_workflow)
            _, val_result = validate_lead_file(path_obj, schema)

            # 4. Record lead rejections in database
            if val_result.rejected_rows:
                repo.record_rejections(
                    drop_id=drop_id,
                    workflow_id=resolved_workflow,
                    batch_id=resolved_batch,
                    rejections=val_result.rejected_rows,
                    conn=connection,
                )

            # 5. Intra-batch deduplication across accepted rows
            fields = schema.get("fields", [])
            dedup_engine = LeadDeduplicationEngine(schema_fields=fields)
            for accepted_row in val_result.accepted_rows:
                dedup_engine.process_accepted_row(resolved_workflow, resolved_batch, accepted_row)

            intra_duplicates = dedup_engine.duplicate_count
            merged_records = dedup_engine.get_result().get_leads_list()

            # 6. Database persistence with cross-batch deduplication
            inserted_count = 0
            updated_count = 0

            for rec in merged_records:
                _, is_inserted = repo.upsert_lead_record(rec, conn=connection)
                if is_inserted:
                    inserted_count += 1
                else:
                    updated_count += 1

            # Total duplicates merged = within-batch duplicates + cross-batch updates
            total_duplicates = intra_duplicates + updated_count

            # 7. Record file checkpoint
            cur.execute(
                """
                INSERT INTO ingestion_file_checkpoints (
                    drop_id, file_type, file_path, file_name, file_hash,
                    status, records_count, processed_at
                ) VALUES (%s, 'LEAD_CSV', %s, %s, %s, 'PROCESSED', %s, CURRENT_TIMESTAMP)
                ON CONFLICT (drop_id, file_path)
                DO UPDATE SET
                    file_hash = EXCLUDED.file_hash,
                    status = EXCLUDED.status,
                    records_count = EXCLUDED.records_count,
                    processed_at = CURRENT_TIMESTAMP;
                """,
                (
                    drop_id,
                    rel_path,
                    path_obj.name,
                    file_hash,
                    val_result.total_nonblank_rows,
                ),
            )

            return LeadIngestionSummary(
                drop_id=drop_id,
                workflow_id=resolved_workflow,
                batch_id=resolved_batch,
                file_path=rel_path,
                file_hash=file_hash,
                status="PROCESSED",
                total_nonblank_rows=val_result.total_nonblank_rows,
                accepted_rows=val_result.accepted_count,
                rejected_rows=val_result.rejected_count,
                duplicate_rows_merged=total_duplicates,
                inserted_leads=inserted_count,
                updated_leads=updated_count,
                warning_count=val_result.total_warnings,
                rejections_by_reason=dict(val_result.rejections_by_reason),
            )

    if conn is not None:
        return _process_in_transaction(conn)
    else:
        with transaction() as auto_conn:
            return _process_in_transaction(auto_conn)
