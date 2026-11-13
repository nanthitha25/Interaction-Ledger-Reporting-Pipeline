"""
Database Migration Framework.

Manages versioned, forward migrations and reversible rollbacks in PostgreSQL.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from psycopg2.extensions import connection as PgConnection

from src.db.connection import get_cursor, transaction

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent


class MigrationError(Exception):
    """Raised when a migration or rollback fails."""


@dataclass(frozen=True)
class MigrationFile:
    version: int
    name: str
    up_path: Path
    down_path: Optional[Path]


@dataclass(frozen=True)
class MigrationStatus:
    version: int
    name: str
    is_applied: bool
    applied_at: Optional[datetime]


class MigrationRunner:
    """
    Manages discovery, execution, tracking, and rollback of schema migrations.
    """

    def __init__(self, migrations_dir: Path = MIGRATIONS_DIR):
        self.migrations_dir = migrations_dir

    def _ensure_migrations_table(self, conn: PgConnection) -> None:
        """Creates the schema_migrations tracking table if it does not exist."""
        with get_cursor(conn) as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INT PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    def discover_migrations(self) -> list[MigrationFile]:
        """Discovers all versioned migration files in the migrations directory."""
        up_files: dict[int, tuple[str, Path]] = {}
        down_files: dict[int, Path] = {}

        for p in self.migrations_dir.iterdir():
            if not p.is_file():
                continue

            # Check for rollback file first
            down_match = re.match(r"^(\d+)_(.+)\.rollback\.sql$", p.name)
            if down_match:
                version = int(down_match.group(1))
                down_files[version] = p
                continue

            # Standard forward migration
            up_match = re.match(r"^(\d+)_(.+)\.sql$", p.name)
            if up_match:
                version = int(up_match.group(1))
                name = up_match.group(2)
                up_files[version] = (name, p)

        migrations: list[MigrationFile] = []
        for version in sorted(up_files.keys()):
            name, up_path = up_files[version]
            down_path = down_files.get(version)
            migrations.append(
                MigrationFile(
                    version=version,
                    name=name,
                    up_path=up_path,
                    down_path=down_path,
                )
            )

        return migrations

    def get_applied_migrations(self, conn: PgConnection) -> dict[int, datetime]:
        """Returns a mapping of applied version -> applied_at timestamp."""
        self._ensure_migrations_table(conn)
        with get_cursor(conn) as cur:
            cur.execute("SELECT version, applied_at FROM schema_migrations ORDER BY version ASC;")
            rows = cur.fetchall()
            return {row[0]: row[1] for row in rows}

    def get_status(self, conn: Optional[PgConnection] = None) -> list[MigrationStatus]:
        """Returns the status of all discovered migrations."""
        if conn is not None:
            return self._get_status_with_conn(conn)
        with transaction() as new_conn:
            return self._get_status_with_conn(new_conn)

    def _get_status_with_conn(self, conn: PgConnection) -> list[MigrationStatus]:
        applied = self.get_applied_migrations(conn)
        discovered = self.discover_migrations()
        statuses: list[MigrationStatus] = []
        for m in discovered:
            is_applied = m.version in applied
            applied_at = applied.get(m.version)
            statuses.append(
                MigrationStatus(
                    version=m.version,
                    name=m.name,
                    is_applied=is_applied,
                    applied_at=applied_at,
                )
            )
        return statuses

    def migrate(
        self,
        conn: Optional[PgConnection] = None,
        target_version: Optional[int] = None,
    ) -> list[int]:
        """
        Applies pending migrations up to target_version (or all if target_version is None).
        Returns list of newly applied version numbers.
        """
        if conn is not None:
            return self._migrate_with_conn(conn, target_version)
        with transaction() as new_conn:
            return self._migrate_with_conn(new_conn, target_version)

    def _migrate_with_conn(
        self,
        conn: PgConnection,
        target_version: Optional[int] = None,
    ) -> list[int]:
        self._ensure_migrations_table(conn)
        applied = self.get_applied_migrations(conn)
        discovered = self.discover_migrations()

        applied_versions: list[int] = []
        for m in discovered:
            if m.version in applied:
                continue
            if target_version is not None and m.version > target_version:
                break

            logger.info("Applying migration %03d_%s ...", m.version, m.name)
            sql_content = m.up_path.read_text(encoding="utf-8")
            try:
                with get_cursor(conn) as cur:
                    cur.execute(sql_content)
                    cur.execute(
                        "INSERT INTO schema_migrations (version, name) VALUES (%s, %s);",
                        (m.version, m.name),
                    )
                applied_versions.append(m.version)
                logger.info("Successfully applied migration %03d_%s.", m.version, m.name)
            except Exception as e:
                logger.error("Migration %03d_%s failed: %s", m.version, m.name, e)
                raise MigrationError(f"Migration {m.version} ({m.name}) failed: {e}") from e

        return applied_versions

    def rollback(
        self,
        conn: Optional[PgConnection] = None,
        steps: int = 1,
        target_version: Optional[int] = None,
    ) -> list[int]:
        """
        Rolls back applied migrations in reverse order.
        Returns list of rolled back version numbers.
        """
        if conn is not None:
            return self._rollback_with_conn(conn, steps, target_version)
        with transaction() as new_conn:
            return self._rollback_with_conn(new_conn, steps, target_version)

    def _rollback_with_conn(
        self,
        conn: PgConnection,
        steps: int = 1,
        target_version: Optional[int] = None,
    ) -> list[int]:
        self._ensure_migrations_table(conn)
        applied = self.get_applied_migrations(conn)
        discovered = {m.version: m for m in self.discover_migrations()}

        sorted_applied = sorted(applied.keys(), reverse=True)
        rolled_back_versions: list[int] = []

        count = 0
        for version in sorted_applied:
            if target_version is not None and version <= target_version:
                break
            if target_version is None and count >= steps:
                break

            m = discovered.get(version)
            if m is None or m.down_path is None or not m.down_path.exists():
                raise MigrationError(f"No rollback script found for version {version}")

            logger.info("Rolling back migration %03d_%s ...", m.version, m.name)
            rollback_sql = m.down_path.read_text(encoding="utf-8")
            try:
                with get_cursor(conn) as cur:
                    cur.execute(rollback_sql)
                    cur.execute("DELETE FROM schema_migrations WHERE version = %s;", (version,))
                rolled_back_versions.append(version)
                count += 1
                logger.info("Successfully rolled back migration %03d_%s.", m.version, m.name)
            except Exception as e:
                logger.error("Rollback of %03d_%s failed: %s", version, m.name, e)
                raise MigrationError(f"Rollback of {version} failed: {e}") from e

        return rolled_back_versions
