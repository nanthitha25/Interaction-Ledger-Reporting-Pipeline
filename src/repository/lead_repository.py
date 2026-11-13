"""
PostgreSQL Lead Repository for Interaction Ledger & Reporting Pipeline.

Provides transactional persistence, idempotent upsert with last-non-null-write-wins semantics,
secure cryptographic isolation of PII at rest via PIIVault, and referential integrity.
Plaintext phone numbers and customer names are never persisted or logged.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
import logging
from typing import Any, Mapping, Optional, Sequence
from uuid import UUID

from psycopg2.extensions import connection as PgConnection

from src.db.connection import get_connection, get_cursor, transaction
from src.ingestion.deduplication import LeadRecord
from src.ingestion.normalizers import normalize_decimal, normalize_indian_phone, parse_date
from src.ingestion.validator import RejectedLeadRow
from src.security.vault import PIIVault

logger = logging.getLogger(__name__)


class LeadRepositoryError(Exception):
    """Base exception for lead repository operations."""
    pass


class LeadNotFoundError(LeadRepositoryError):
    """Raised when a requested lead is not found."""
    pass


class LeadValidationError(LeadRepositoryError):
    """Raised when lead data fails repository validation prior to persistence."""
    pass


class LeadModel:
    """
    Domain entity representing a persisted lead record in the PostgreSQL database.
    Stores only encrypted PII and blind indexes at rest.
    """

    __slots__ = (
        "lead_id",
        "workflow_id",
        "phone_bidx",
        "phone_encrypted",
        "customer_name_encrypted",
        "external_ref",
        "batch_id",
        "policy_expiry",
        "premium_amount",
        "order_date",
        "product",
        "preferred_language",
        "created_at",
        "updated_at",
    )

    def __init__(
        self,
        lead_id: str,
        workflow_id: str,
        phone_bidx: str,
        phone_encrypted: str,
        customer_name_encrypted: str,
        external_ref: str,
        batch_id: str,
        policy_expiry: Optional[datetime.date] = None,
        premium_amount: Optional[Decimal] = None,
        order_date: Optional[datetime.date] = None,
        product: Optional[str] = None,
        preferred_language: str = "en",
        created_at: Optional[datetime.datetime] = None,
        updated_at: Optional[datetime.datetime] = None,
    ):
        self.lead_id = str(lead_id)
        self.workflow_id = workflow_id
        self.phone_bidx = phone_bidx
        self.phone_encrypted = phone_encrypted
        self.customer_name_encrypted = customer_name_encrypted
        self.external_ref = external_ref
        self.batch_id = batch_id
        self.policy_expiry = policy_expiry
        self.premium_amount = premium_amount
        self.order_date = order_date
        self.product = product
        self.preferred_language = preferred_language
        self.created_at = created_at
        self.updated_at = updated_at

    def to_dict(self) -> dict[str, Any]:
        """Returns a dictionary representation of the lead model."""
        return {
            "lead_id": self.lead_id,
            "workflow_id": self.workflow_id,
            "phone_bidx": self.phone_bidx,
            "phone_encrypted": self.phone_encrypted,
            "customer_name_encrypted": self.customer_name_encrypted,
            "external_ref": self.external_ref,
            "batch_id": self.batch_id,
            "policy_expiry": self.policy_expiry.isoformat() if self.policy_expiry else None,
            "premium_amount": float(self.premium_amount) if self.premium_amount is not None else None,
            "order_date": self.order_date.isoformat() if self.order_date else None,
            "product": self.product,
            "preferred_language": self.preferred_language,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:
        return (
            f"<LeadModel lead_id={self.lead_id!r} workflow={self.workflow_id!r} "
            f"phone_bidx={self.phone_bidx[:8]}... batch_id={self.batch_id!r}>"
        )


class LeadRepository:
    """
    Repository providing transactional data access and idempotent upsert
    for the PostgreSQL 'leads' table.
    """

    def __init__(self, vault: Optional[PIIVault] = None):
        """
        Initializes LeadRepository with a PIIVault instance.

        Args:
            vault: Optional PIIVault instance. If omitted, initializes default PIIVault.
        """
        self._vault = vault or PIIVault()

    @property
    def vault(self) -> PIIVault:
        """Returns the configured PIIVault."""
        return self._vault

    def decrypt_phone(self, lead: LeadModel) -> str:
        """Decrypts and returns the canonical plaintext phone number for a lead."""
        return self._vault.decrypt_phone(lead.phone_encrypted)

    def decrypt_customer_name(self, lead: LeadModel) -> str:
        """Decrypts and returns the plaintext customer name for a lead."""
        return self._vault.decrypt_customer_name(lead.customer_name_encrypted)

    def mask_phone(self, lead: LeadModel) -> str:
        """Returns the masked phone number (+91******XXXX) for a lead."""
        decrypted = self.decrypt_phone(lead)
        return self._vault.mask_phone(decrypted)

    def upsert_lead(
        self,
        workflow_id: str,
        batch_id: str,
        phone: Optional[str] = None,
        customer_name: Optional[str] = None,
        external_ref: Optional[str] = None,
        policy_expiry: Optional[datetime.date | str] = None,
        premium_amount: Optional[Decimal | float | int | str] = None,
        order_date: Optional[datetime.date | str] = None,
        product: Optional[str] = None,
        preferred_language: Optional[str] = None,
        lead_id: Optional[str | UUID] = None,
        phone_bidx: Optional[str] = None,
        phone_encrypted: Optional[str] = None,
        customer_name_encrypted: Optional[str] = None,
        default_preferred_language: str = "en",
        conn: Optional[PgConnection] = None,
    ) -> tuple[LeadModel, bool]:
        """
        Atomically inserts or updates a lead record using last-non-null-write-wins semantics.

        Args:
            workflow_id: Workflow identifier ('motor_renewal' or 'policy_feedback').
            batch_id: File stem of the originating batch (immutable on first write).
            phone: Plaintext or canonical phone number (encrypted prior to insert).
            customer_name: Plaintext customer name (encrypted prior to insert).
            external_ref: Domain external reference (e.g. policy_number or order_id).
            policy_expiry: Date or ISO string for policy expiry (motor_renewal).
            premium_amount: Premium amount (motor_renewal).
            order_date: Date or ISO string for order date (policy_feedback).
            product: Product name (policy_feedback).
            preferred_language: Explicit preferred language, or None if omitted in current batch.
            lead_id: Optional pre-assigned UUID (otherwise generated by PostgreSQL).
            phone_bidx: Pre-calculated HMAC blind index (optional if phone provided).
            phone_encrypted: Pre-encrypted phone ciphertext (optional if phone provided).
            customer_name_encrypted: Pre-encrypted name ciphertext (optional if customer_name provided).
            default_preferred_language: Schema fallback for new leads (default: 'en').
            conn: Optional active database connection (for multi-statement transactions).

        Returns:
            tuple[LeadModel, bool]: (persisted_lead, is_inserted)
                is_inserted is True if a new row was inserted, False if an existing row was updated.
        """
        if not workflow_id or not workflow_id.strip():
            raise LeadValidationError("workflow_id cannot be empty.")
        if not batch_id or not batch_id.strip():
            raise LeadValidationError("batch_id cannot be empty.")

        # Resolve phone blind index and ciphertext
        if phone_bidx is None or phone_encrypted is None:
            if not phone:
                raise LeadValidationError("Either phone or (phone_bidx and phone_encrypted) must be provided.")
            canonical_phone = normalize_indian_phone(phone)
            phone_bidx = self._vault.get_phone_blind_index(canonical_phone)
            phone_encrypted = self._vault.encrypt_phone(canonical_phone)

        # Resolve customer name ciphertext
        if customer_name_encrypted is None and customer_name is not None:
            trimmed_name = customer_name.strip()
            if trimmed_name:
                customer_name_encrypted = self._vault.encrypt_customer_name(trimmed_name)

        # Normalize dates
        parsed_policy_expiry: Optional[datetime.date] = None
        if policy_expiry is not None:
            if isinstance(policy_expiry, datetime.date):
                parsed_policy_expiry = policy_expiry
            elif isinstance(policy_expiry, str) and policy_expiry.strip():
                parsed_policy_expiry = parse_date(policy_expiry)

        parsed_order_date: Optional[datetime.date] = None
        if order_date is not None:
            if isinstance(order_date, datetime.date):
                parsed_order_date = order_date
            elif isinstance(order_date, str) and order_date.strip():
                parsed_order_date = parse_date(order_date)

        # Normalize premium amount
        parsed_premium: Optional[Decimal] = None
        if premium_amount is not None:
            if isinstance(premium_amount, Decimal):
                parsed_premium = premium_amount
            elif isinstance(premium_amount, (int, float)):
                parsed_premium = Decimal(str(premium_amount))
            elif isinstance(premium_amount, str) and premium_amount.strip():
                parsed_premium = normalize_decimal(premium_amount)

        # Normalize preferred language: None if missing, lowercase enum if provided
        explicit_lang: Optional[str] = None
        if preferred_language is not None and str(preferred_language).strip():
            explicit_lang = str(preferred_language).strip().lower()

        # External reference
        trimmed_external_ref: Optional[str] = None
        if external_ref is not None:
            trimmed_external_ref = str(external_ref).strip()

        # Prepare SQL statement
        query = """
            INSERT INTO leads (
                lead_id,
                workflow_id,
                phone_bidx,
                phone_encrypted,
                customer_name_encrypted,
                external_ref,
                batch_id,
                policy_expiry,
                premium_amount,
                order_date,
                product,
                preferred_language,
                created_at,
                updated_at
            ) VALUES (
                COALESCE(%(lead_id)s, gen_random_uuid()),
                %(workflow_id)s,
                %(phone_bidx)s,
                %(phone_encrypted)s,
                %(customer_name_encrypted)s,
                %(external_ref)s,
                %(batch_id)s,
                %(policy_expiry)s,
                %(premium_amount)s,
                %(order_date)s,
                %(product)s,
                COALESCE(%(explicit_preferred_language)s, %(default_preferred_language)s, 'en'),
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            )
            ON CONFLICT (workflow_id, phone_bidx) DO UPDATE SET
                phone_encrypted = EXCLUDED.phone_encrypted,
                customer_name_encrypted = COALESCE(EXCLUDED.customer_name_encrypted, leads.customer_name_encrypted),
                external_ref = COALESCE(EXCLUDED.external_ref, leads.external_ref),
                policy_expiry = COALESCE(EXCLUDED.policy_expiry, leads.policy_expiry),
                premium_amount = COALESCE(EXCLUDED.premium_amount, leads.premium_amount),
                order_date = COALESCE(EXCLUDED.order_date, leads.order_date),
                product = COALESCE(EXCLUDED.product, leads.product),
                preferred_language = COALESCE(%(explicit_preferred_language)s, leads.preferred_language),
                updated_at = CURRENT_TIMESTAMP
            RETURNING
                lead_id,
                workflow_id,
                phone_bidx,
                phone_encrypted,
                customer_name_encrypted,
                external_ref,
                batch_id,
                policy_expiry,
                premium_amount,
                order_date,
                product,
                preferred_language,
                created_at,
                updated_at,
                (xmax = 0) AS is_inserted;
        """

        params = {
            "lead_id": str(lead_id) if lead_id is not None else None,
            "workflow_id": workflow_id.strip(),
            "phone_bidx": phone_bidx,
            "phone_encrypted": phone_encrypted,
            "customer_name_encrypted": customer_name_encrypted,
            "external_ref": trimmed_external_ref,
            "batch_id": batch_id.strip(),
            "policy_expiry": parsed_policy_expiry,
            "premium_amount": parsed_premium,
            "order_date": parsed_order_date,
            "product": product.strip() if product else None,
            "explicit_preferred_language": explicit_lang,
            "default_preferred_language": default_preferred_language,
        }

        def _execute(connection: PgConnection) -> tuple[LeadModel, bool]:
            with get_cursor(connection) as cur:
                cur.execute(query, params)
                row = cur.fetchone()
                if not row:
                    raise LeadRepositoryError("Upsert completed without returning a row.")

                model = LeadModel(
                    lead_id=row[0],
                    workflow_id=row[1],
                    phone_bidx=row[2],
                    phone_encrypted=row[3],
                    customer_name_encrypted=row[4],
                    external_ref=row[5],
                    batch_id=row[6],
                    policy_expiry=row[7],
                    premium_amount=row[8],
                    order_date=row[9],
                    product=row[10],
                    preferred_language=row[11],
                    created_at=row[12],
                    updated_at=row[13],
                )
                is_inserted = bool(row[14])
                return model, is_inserted

        if conn is not None:
            return _execute(conn)
        else:
            with transaction() as auto_conn:
                return _execute(auto_conn)

    def upsert_lead_record(
        self,
        lead_record: LeadRecord,
        conn: Optional[PgConnection] = None,
    ) -> tuple[LeadModel, bool]:
        """
        Upserts a merged LeadRecord instance from the deduplication engine into PostgreSQL.

        Args:
            lead_record: LeadRecord instance containing identity, permanent batch_id, and values.
            conn: Optional database connection for transaction sharing.

        Returns:
            tuple[LeadModel, bool]: (persisted_lead, is_inserted)
        """
        workflow_id = lead_record.workflow_id
        batch_id = lead_record.batch_id
        canonical_phone = lead_record.phone
        values = lead_record.values

        customer_name = values.get("customer_name")
        external_ref = (
            values.get("external_ref")
            or values.get("policy_number")
            or values.get("order_id")
        )
        policy_expiry = values.get("policy_expiry")
        premium_amount = values.get("premium_amount")
        order_date = values.get("order_date")
        product = values.get("product")
        preferred_language = values.get("preferred_language")

        return self.upsert_lead(
            workflow_id=workflow_id,
            batch_id=batch_id,
            phone=canonical_phone,
            customer_name=customer_name,
            external_ref=external_ref,
            policy_expiry=policy_expiry,
            premium_amount=premium_amount,
            order_date=order_date,
            product=product,
            preferred_language=preferred_language,
            conn=conn,
        )

    def upsert_lead_records(
        self,
        records: Sequence[LeadRecord],
        conn: Optional[PgConnection] = None,
    ) -> list[tuple[LeadModel, bool]]:
        """
        Upserts a collection of LeadRecords in a single atomic database transaction.

        Args:
            records: Sequence of LeadRecord objects.
            conn: Optional database connection.

        Returns:
            List of (LeadModel, is_inserted) tuples matching input order.
        """
        results: list[tuple[LeadModel, bool]] = []

        def _execute_all(connection: PgConnection) -> list[tuple[LeadModel, bool]]:
            for rec in records:
                res = self.upsert_lead_record(rec, conn=connection)
                results.append(res)
            return results

        if conn is not None:
            return _execute_all(conn)
        else:
            with transaction() as auto_conn:
                return _execute_all(auto_conn)

    def find_by_identity(
        self,
        workflow_id: str,
        phone_bidx: str,
        conn: Optional[PgConnection] = None,
    ) -> Optional[LeadModel]:
        """
        Finds a lead by composite identity: (workflow_id, phone_bidx).

        Args:
            workflow_id: Workflow identifier.
            phone_bidx: 64-character HMAC blind index.
            conn: Optional active database connection.

        Returns:
            LeadModel if found, None otherwise.
        """
        query = """
            SELECT
                lead_id,
                workflow_id,
                phone_bidx,
                phone_encrypted,
                customer_name_encrypted,
                external_ref,
                batch_id,
                policy_expiry,
                premium_amount,
                order_date,
                product,
                preferred_language,
                created_at,
                updated_at
            FROM leads
            WHERE workflow_id = %s AND phone_bidx = %s;
        """

        def _execute(connection: PgConnection) -> Optional[LeadModel]:
            with get_cursor(connection) as cur:
                cur.execute(query, (workflow_id, phone_bidx))
                row = cur.fetchone()
                if not row:
                    return None
                return LeadModel(
                    lead_id=row[0],
                    workflow_id=row[1],
                    phone_bidx=row[2],
                    phone_encrypted=row[3],
                    customer_name_encrypted=row[4],
                    external_ref=row[5],
                    batch_id=row[6],
                    policy_expiry=row[7],
                    premium_amount=row[8],
                    order_date=row[9],
                    product=row[10],
                    preferred_language=row[11],
                    created_at=row[12],
                    updated_at=row[13],
                )

        if conn is not None:
            return _execute(conn)
        else:
            with get_connection() as auto_conn:
                return _execute(auto_conn)

    def find_by_phone(
        self,
        workflow_id: str,
        phone: str,
        conn: Optional[PgConnection] = None,
    ) -> Optional[LeadModel]:
        """
        Finds a lead by workflow ID and raw or canonical phone number.
        The phone number is normalized and converted to HMAC blind index before query.

        Args:
            workflow_id: Workflow identifier.
            phone: Raw or canonical phone number.
            conn: Optional database connection.

        Returns:
            LeadModel if found, None otherwise.
        """
        canonical_phone = normalize_indian_phone(phone)
        phone_bidx = self._vault.get_phone_blind_index(canonical_phone)
        return self.find_by_identity(workflow_id, phone_bidx, conn=conn)

    def find_by_id(
        self,
        lead_id: str | UUID,
        conn: Optional[PgConnection] = None,
    ) -> Optional[LeadModel]:
        """
        Finds a lead by primary key UUID.

        Args:
            lead_id: UUID string or object.
            conn: Optional database connection.

        Returns:
            LeadModel if found, None otherwise.
        """
        query = """
            SELECT
                lead_id,
                workflow_id,
                phone_bidx,
                phone_encrypted,
                customer_name_encrypted,
                external_ref,
                batch_id,
                policy_expiry,
                premium_amount,
                order_date,
                product,
                preferred_language,
                created_at,
                updated_at
            FROM leads
            WHERE lead_id = %s;
        """

        def _execute(connection: PgConnection) -> Optional[LeadModel]:
            with get_cursor(connection) as cur:
                cur.execute(query, (str(lead_id),))
                row = cur.fetchone()
                if not row:
                    return None
                return LeadModel(
                    lead_id=row[0],
                    workflow_id=row[1],
                    phone_bidx=row[2],
                    phone_encrypted=row[3],
                    customer_name_encrypted=row[4],
                    external_ref=row[5],
                    batch_id=row[6],
                    policy_expiry=row[7],
                    premium_amount=row[8],
                    order_date=row[9],
                    product=row[10],
                    preferred_language=row[11],
                    created_at=row[12],
                    updated_at=row[13],
                )

        if conn is not None:
            return _execute(conn)
        else:
            with get_connection() as auto_conn:
                return _execute(auto_conn)

    def count_leads(
        self,
        workflow_id: Optional[str] = None,
        conn: Optional[PgConnection] = None,
    ) -> int:
        """
        Returns the total number of leads, optionally filtered by workflow.

        Args:
            workflow_id: Optional workflow filter.
            conn: Optional database connection.

        Returns:
            Integer count of leads.
        """
        if workflow_id:
            query = "SELECT COUNT(*) FROM leads WHERE workflow_id = %s;"
            params = (workflow_id,)
        else:
            query = "SELECT COUNT(*) FROM leads;"
            params = ()

        def _execute(connection: PgConnection) -> int:
            with get_cursor(connection) as cur:
                cur.execute(query, params)
                row = cur.fetchone()
                return row[0] if row else 0

        if conn is not None:
            return _execute(conn)
        else:
            with get_connection() as auto_conn:
                return _execute(auto_conn)

    def list_leads(
        self,
        workflow_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        conn: Optional[PgConnection] = None,
    ) -> list[LeadModel]:
        """
        Lists leads ordered by created_at, optionally filtered by workflow.

        Args:
            workflow_id: Optional workflow filter.
            limit: Maximum rows to return.
            offset: Rows to skip.
            conn: Optional database connection.

        Returns:
            List of LeadModel instances.
        """
        if workflow_id:
            query = """
                SELECT
                    lead_id, workflow_id, phone_bidx, phone_encrypted,
                    customer_name_encrypted, external_ref, batch_id,
                    policy_expiry, premium_amount, order_date, product,
                    preferred_language, created_at, updated_at
                FROM leads
                WHERE workflow_id = %s
                ORDER BY created_at ASC
                LIMIT %s OFFSET %s;
            """
            params = (workflow_id, limit, offset)
        else:
            query = """
                SELECT
                    lead_id, workflow_id, phone_bidx, phone_encrypted,
                    customer_name_encrypted, external_ref, batch_id,
                    policy_expiry, premium_amount, order_date, product,
                    preferred_language, created_at, updated_at
                FROM leads
                ORDER BY created_at ASC
                LIMIT %s OFFSET %s;
            """
            params = (limit, offset)

        def _execute(connection: PgConnection) -> list[LeadModel]:
            with get_cursor(connection) as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
                return [
                    LeadModel(
                        lead_id=r[0],
                        workflow_id=r[1],
                        phone_bidx=r[2],
                        phone_encrypted=r[3],
                        customer_name_encrypted=r[4],
                        external_ref=r[5],
                        batch_id=r[6],
                        policy_expiry=r[7],
                        premium_amount=r[8],
                        order_date=r[9],
                        product=r[10],
                        preferred_language=r[11],
                        created_at=r[12],
                        updated_at=r[13],
                    )
                    for r in rows
                ]

        if conn is not None:
            return _execute(conn)
        else:
            with get_connection() as auto_conn:
                return _execute(auto_conn)

    def record_rejections(
        self,
        drop_id: str,
        workflow_id: str,
        batch_id: str,
        rejections: Sequence[RejectedLeadRow],
        conn: Optional[PgConnection] = None,
    ) -> int:
        """
        Persists validation rejection records to the 'lead_rejections' table.
        Rejections are idempotent on (drop_id, workflow_id, batch_id, row_number).

        Args:
            drop_id: Current drop identifier.
            workflow_id: Workflow identifier.
            batch_id: Batch file stem.
            rejections: List of RejectedLeadRow instances.
            conn: Optional database connection.

        Returns:
            Number of rejection records persisted.
        """
        if not rejections:
            return 0

        query = """
            INSERT INTO lead_rejections (
                drop_id,
                workflow_id,
                batch_id,
                row_number,
                reason,
                created_at
            ) VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (drop_id, workflow_id, batch_id, row_number)
            DO UPDATE SET reason = EXCLUDED.reason;
        """

        def _execute(connection: PgConnection) -> int:
            with get_cursor(connection) as cur:
                count = 0
                for rej in rejections:
                    cur.execute(query, (drop_id, workflow_id, batch_id, rej.row_number, rej.reason))
                    count += 1
                return count

        if conn is not None:
            return _execute(conn)
        else:
            with transaction() as auto_conn:
                return _execute(auto_conn)

    def get_rejections(
        self,
        drop_id: Optional[str] = None,
        batch_id: Optional[str] = None,
        conn: Optional[PgConnection] = None,
    ) -> list[dict[str, Any]]:
        """
        Retrieves rejection records, optionally filtered by drop_id or batch_id.

        Args:
            drop_id: Optional drop_id filter.
            batch_id: Optional batch_id filter.
            conn: Optional database connection.

        Returns:
            List of rejection dictionaries.
        """
        clauses = []
        params = []
        if drop_id:
            clauses.append("drop_id = %s")
            params.append(drop_id)
        if batch_id:
            clauses.append("batch_id = %s")
            params.append(batch_id)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"""
            SELECT id, drop_id, workflow_id, batch_id, row_number, reason, created_at
            FROM lead_rejections
            {where_sql}
            ORDER BY id ASC;
        """

        def _execute(connection: PgConnection) -> list[dict[str, Any]]:
            with get_cursor(connection) as cur:
                cur.execute(query, tuple(params))
                rows = cur.fetchall()
                return [
                    {
                        "id": r[0],
                        "drop_id": r[1],
                        "workflow_id": r[2],
                        "batch_id": r[3],
                        "row_number": r[4],
                        "reason": r[5],
                        "created_at": r[6],
                    }
                    for r in rows
                ]

        if conn is not None:
            return _execute(conn)
        else:
            with get_connection() as auto_conn:
                return _execute(auto_conn)
