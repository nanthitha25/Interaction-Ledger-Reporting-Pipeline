"""
PostgreSQL Integration Test Suite for DB-02.

Executes and verifies all 16 integration test scenarios (IT-01 through IT-16)
against a live PostgreSQL test database, enforcing ledger immutability,
referential integrity, unique constraints, and append-only invariants.
"""

import json
import pytest
import psycopg2
from psycopg2 import errors

from src.db.connection import close_pool, get_connection, get_cursor, transaction
from src.db.migrations.runner import MigrationRunner


@pytest.fixture(scope="module", autouse=True)
def apply_migrations():
    """Ensures migrations are applied before running integration tests."""
    close_pool()
    runner = MigrationRunner()
    runner.migrate()
    yield
    close_pool()


@pytest.fixture(autouse=True)
def clean_database():
    """Cleans up data before and after each integration test."""
    def _truncate():
        with get_connection() as conn:
            with get_cursor(conn) as cur:
                # Temporarily disable statement-level truncate trigger to allow test harness cleanup
                cur.execute("ALTER TABLE interaction_ledger DISABLE TRIGGER trg_ledger_prevent_truncate;")
                cur.execute("ALTER TABLE interaction_ledger DISABLE TRIGGER trg_ledger_prevent_delete;")
                cur.execute("TRUNCATE TABLE interaction_ledger CASCADE;")
                cur.execute("TRUNCATE TABLE lead_rejections CASCADE;")
                cur.execute("TRUNCATE TABLE leads CASCADE;")
                cur.execute("TRUNCATE TABLE dispositions CASCADE;")
                cur.execute("TRUNCATE TABLE ingestion_file_checkpoints CASCADE;")
                cur.execute("TRUNCATE TABLE ingestion_runs CASCADE;")
                cur.execute("ALTER TABLE interaction_ledger ENABLE TRIGGER trg_ledger_prevent_truncate;")
                cur.execute("ALTER TABLE interaction_ledger ENABLE TRIGGER trg_ledger_prevent_delete;")
            conn.commit()

    _truncate()
    yield
    _truncate()


def _seed_base_data(conn):
    """Helper to seed a drop run and standard dispositions."""
    with get_cursor(conn) as cur:
        cur.execute(
            """
            INSERT INTO ingestion_runs (drop_id, ingest_order, status)
            VALUES ('drop_test_1', 1, 'IN_PROGRESS')
            ON CONFLICT (drop_id) DO NOTHING;
            """
        )
        cur.execute(
            """
            INSERT INTO dispositions (workflow_id, code, label, is_terminal, is_success)
            VALUES 
                ('motor_renewal', 'renewed', 'Policy Renewed', TRUE, TRUE),
                ('motor_renewal', 'callback', 'Callback Requested', FALSE, FALSE),
                ('policy_feedback', 'feedback_received', 'Feedback Received', TRUE, TRUE)
            ON CONFLICT (workflow_id, code) DO NOTHING;
            """
        )


def _create_lead(conn, workflow_id="motor_renewal", phone_bidx="hash_customer_1", name="Test Customer"):
    with get_cursor(conn) as cur:
        cur.execute(
            """
            INSERT INTO leads (
                workflow_id, phone_bidx, phone_encrypted, customer_name_encrypted,
                external_ref, batch_id
            ) VALUES (
                %s, %s, 'enc_phone', %s, 'REF123', 'batch_001'
            ) RETURNING lead_id;
            """,
            (workflow_id, phone_bidx, name),
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# IT-01: Valid orphan-to-linked transition
# ---------------------------------------------------------------------------
def test_it_01_valid_orphan_to_linked_transition():
    with transaction() as conn:
        _seed_base_data(conn)
        lead_id = _create_lead(conn, workflow_id="motor_renewal", phone_bidx="phone_bidx_01")

        with get_cursor(conn) as cur:
            # 1. Insert orphan event
            cur.execute(
                """
                INSERT INTO interaction_ledger (
                    event_id, event_type, workflow_id, call_id, phone_bidx,
                    phone_encrypted, occurred_at, payload_json, is_orphan, lead_id, drop_id
                ) VALUES (
                    'evt_01', 'call.initiated', 'motor_renewal', 'call_01', 'phone_bidx_01',
                    'enc_phone', NOW(), '{"channel": "voice"}'::jsonb, TRUE, NULL, 'drop_test_1'
                );
                """
            )

            # 2. Reconcile orphan to lead
            cur.execute(
                """
                UPDATE interaction_ledger
                SET lead_id = %s, is_orphan = FALSE
                WHERE event_id = 'evt_01';
                """,
                (lead_id,),
            )

            # 3. Assert state
            cur.execute("SELECT lead_id, is_orphan FROM interaction_ledger WHERE event_id = 'evt_01';")
            row = cur.fetchone()
            assert str(row[0]) == str(lead_id)
            assert row[1] is False


# ---------------------------------------------------------------------------
# IT-02: Cross-workflow orphan-linking rejection
# ---------------------------------------------------------------------------
def test_it_02_cross_workflow_orphan_linking_rejection():
    with get_connection() as conn:
        _seed_base_data(conn)
        lead_id = _create_lead(conn, workflow_id="policy_feedback", phone_bidx="phone_bidx_02")

        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO interaction_ledger (
                    event_id, event_type, workflow_id, call_id, phone_bidx,
                    occurred_at, payload_json, is_orphan, lead_id, drop_id
                ) VALUES (
                    'evt_02', 'call.initiated', 'motor_renewal', 'call_02', 'phone_bidx_02',
                    NOW(), '{}'::jsonb, TRUE, NULL, 'drop_test_1'
                );
                """
            )
        conn.commit()

        # Attempt linking across workflows
        with pytest.raises(psycopg2.Error, match="Cross-workflow violation"):
            with get_cursor(conn) as cur:
                cur.execute(
                    "UPDATE interaction_ledger SET lead_id = %s, is_orphan = FALSE WHERE event_id = 'evt_02';",
                    (lead_id,),
                )
        conn.rollback()


# ---------------------------------------------------------------------------
# IT-03: Wrong-customer orphan-linking rejection
# ---------------------------------------------------------------------------
def test_it_03_wrong_customer_orphan_linking_rejection():
    with get_connection() as conn:
        _seed_base_data(conn)
        # Lead has phone_bidx_customer_B
        lead_id = _create_lead(conn, workflow_id="motor_renewal", phone_bidx="phone_bidx_customer_B")

        with get_cursor(conn) as cur:
            # Event has phone_bidx_customer_A
            cur.execute(
                """
                INSERT INTO interaction_ledger (
                    event_id, event_type, workflow_id, call_id, phone_bidx,
                    occurred_at, payload_json, is_orphan, lead_id, drop_id
                ) VALUES (
                    'evt_03', 'call.initiated', 'motor_renewal', 'call_03', 'phone_bidx_customer_A',
                    NOW(), '{}'::jsonb, TRUE, NULL, 'drop_test_1'
                );
                """
            )
        conn.commit()

        with pytest.raises(psycopg2.Error, match="Phone mismatch violation"):
            with get_cursor(conn) as cur:
                cur.execute(
                    "UPDATE interaction_ledger SET lead_id = %s, is_orphan = FALSE WHERE event_id = 'evt_03';",
                    (lead_id,),
                )
        conn.rollback()


# ---------------------------------------------------------------------------
# IT-04: Override linking to correct call-associated lead
# ---------------------------------------------------------------------------
def test_it_04_override_linking_to_correct_call_associated_lead():
    with transaction() as conn:
        _seed_base_data(conn)
        lead_id = _create_lead(conn, workflow_id="motor_renewal", phone_bidx="phone_bidx_04")

        with get_cursor(conn) as cur:
            # Preceding call event linked to lead
            cur.execute(
                """
                INSERT INTO interaction_ledger (
                    event_id, event_type, workflow_id, call_id, phone_bidx,
                    occurred_at, payload_json, is_orphan, lead_id, drop_id
                ) VALUES (
                    'evt_call_04', 'call.initiated', 'motor_renewal', 'call_04', 'phone_bidx_04',
                    NOW(), '{}'::jsonb, FALSE, %s, 'drop_test_1'
                );
                """,
                (lead_id,),
            )

            # Orphan override event without phone
            cur.execute(
                """
                INSERT INTO interaction_ledger (
                    event_id, event_type, workflow_id, call_id, phone_bidx,
                    disposition_code, occurred_at, payload_json, is_orphan, lead_id, drop_id
                ) VALUES (
                    'evt_ovr_04', 'disposition.override', 'motor_renewal', 'call_04', NULL,
                    'renewed', NOW(), '{}'::jsonb, TRUE, NULL, 'drop_test_1'
                );
                """
            )

            # Link override to lead_id associated with call_04
            cur.execute(
                """
                UPDATE interaction_ledger
                SET lead_id = %s, is_orphan = FALSE
                WHERE event_id = 'evt_ovr_04';
                """,
                (lead_id,),
            )

            cur.execute("SELECT lead_id, is_orphan FROM interaction_ledger WHERE event_id = 'evt_ovr_04';")
            row = cur.fetchone()
            assert str(row[0]) == str(lead_id)
            assert row[1] is False


# ---------------------------------------------------------------------------
# IT-05: Override linking to unrelated lead
# ---------------------------------------------------------------------------
def test_it_05_override_linking_to_unrelated_lead():
    with get_connection() as conn:
        _seed_base_data(conn)
        lead_A = _create_lead(conn, workflow_id="motor_renewal", phone_bidx="phone_lead_A")
        lead_B = _create_lead(conn, workflow_id="motor_renewal", phone_bidx="phone_lead_B")

        with get_cursor(conn) as cur:
            # Call is linked to lead_A
            cur.execute(
                """
                INSERT INTO interaction_ledger (
                    event_id, event_type, workflow_id, call_id, phone_bidx,
                    occurred_at, payload_json, is_orphan, lead_id, drop_id
                ) VALUES (
                    'evt_call_05', 'call.initiated', 'motor_renewal', 'call_05', 'phone_lead_A',
                    NOW(), '{}'::jsonb, FALSE, %s, 'drop_test_1'
                );
                """,
                (lead_A,),
            )

            # Orphan override for call_05
            cur.execute(
                """
                INSERT INTO interaction_ledger (
                    event_id, event_type, workflow_id, call_id, phone_bidx,
                    disposition_code, occurred_at, payload_json, is_orphan, lead_id, drop_id
                ) VALUES (
                    'evt_ovr_05', 'disposition.override', 'motor_renewal', 'call_05', NULL,
                    'renewed', NOW(), '{}'::jsonb, TRUE, NULL, 'drop_test_1'
                );
                """
            )
        conn.commit()

        # Attempt to link override to unrelated lead_B
        with pytest.raises(psycopg2.Error, match="Override association mismatch"):
            with get_cursor(conn) as cur:
                cur.execute(
                    "UPDATE interaction_ledger SET lead_id = %s, is_orphan = FALSE WHERE event_id = 'evt_ovr_05';",
                    (lead_B,),
                )
        conn.rollback()


# ---------------------------------------------------------------------------
# IT-06: Multiple or conflicting lead associations for a call ID
# ---------------------------------------------------------------------------
def test_it_06_multiple_or_conflicting_lead_associations_for_call_id():
    with get_connection() as conn:
        _seed_base_data(conn)
        lead_1 = _create_lead(conn, workflow_id="motor_renewal", phone_bidx="phone_lead_1")
        lead_2 = _create_lead(conn, workflow_id="motor_renewal", phone_bidx="phone_lead_2")

        with get_cursor(conn) as cur:
            # Corrupted state: same call_id linked to two distinct leads
            cur.execute(
                """
                INSERT INTO interaction_ledger (
                    event_id, event_type, workflow_id, call_id, phone_bidx,
                    occurred_at, payload_json, is_orphan, lead_id, drop_id
                ) VALUES 
                    ('evt_call_06a', 'call.initiated', 'motor_renewal', 'call_06', 'phone_lead_1', NOW(), '{}'::jsonb, FALSE, %s, 'drop_test_1'),
                    ('evt_call_06b', 'call.connected', 'motor_renewal', 'call_06', 'phone_lead_2', NOW(), '{}'::jsonb, FALSE, %s, 'drop_test_1');
                """,
                (lead_1, lead_2),
            )

            # Orphan override for call_06
            cur.execute(
                """
                INSERT INTO interaction_ledger (
                    event_id, event_type, workflow_id, call_id, phone_bidx,
                    disposition_code, occurred_at, payload_json, is_orphan, lead_id, drop_id
                ) VALUES (
                    'evt_ovr_06', 'disposition.override', 'motor_renewal', 'call_06', NULL,
                    'renewed', NOW(), '{}'::jsonb, TRUE, NULL, 'drop_test_1'
                );
                """
            )
        conn.commit()

        # Attempt to link override to either lead
        with pytest.raises(psycopg2.Error, match="Ambiguous call association: Multiple conflicting leads"):
            with get_cursor(conn) as cur:
                cur.execute(
                    "UPDATE interaction_ledger SET lead_id = %s, is_orphan = FALSE WHERE event_id = 'evt_ovr_06';",
                    (lead_1,),
                )
        conn.rollback()


# ---------------------------------------------------------------------------
# IT-07: Attempt to modify an already-linked event
# ---------------------------------------------------------------------------
def test_it_07_attempt_to_modify_already_linked_event():
    with get_connection() as conn:
        _seed_base_data(conn)
        lead_id = _create_lead(conn, workflow_id="motor_renewal", phone_bidx="phone_bidx_07")

        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO interaction_ledger (
                    event_id, event_type, workflow_id, call_id, phone_bidx,
                    occurred_at, payload_json, is_orphan, lead_id, drop_id
                ) VALUES (
                    'evt_07', 'call.initiated', 'motor_renewal', 'call_07', 'phone_bidx_07',
                    NOW(), '{}'::jsonb, FALSE, %s, 'drop_test_1'
                );
                """,
                (lead_id,),
            )
        conn.commit()

        # Attempt any update on already linked event
        with pytest.raises(psycopg2.Error, match="Already-linked events are permanently immutable"):
            with get_cursor(conn) as cur:
                cur.execute(
                    "UPDATE interaction_ledger SET payload_json = '{\"tamper\": true}'::jsonb WHERE event_id = 'evt_07';"
                )
        conn.rollback()


# ---------------------------------------------------------------------------
# IT-08: Immutable nullable-column modification attempts
# ---------------------------------------------------------------------------
def test_it_08_immutable_nullable_column_modification_attempts():
    with get_connection() as conn:
        _seed_base_data(conn)
        lead_id = _create_lead(conn, workflow_id="motor_renewal", phone_bidx="phone_bidx_08")

        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO interaction_ledger (
                    event_id, event_type, workflow_id, call_id, phone_bidx, disposition_code,
                    occurred_at, payload_json, is_orphan, lead_id, drop_id
                ) VALUES (
                    'evt_08', 'call.initiated', 'motor_renewal', 'call_08', NULL, NULL,
                    NOW(), '{}'::jsonb, TRUE, NULL, 'drop_test_1'
                );
                """
            )
        conn.commit()

        # Attempt to simultaneously link and inject a disposition_code (modifying nullable column)
        with pytest.raises(psycopg2.Error, match="Immutable ledger violation"):
            with get_cursor(conn) as cur:
                cur.execute(
                    """
                    UPDATE interaction_ledger
                    SET lead_id = %s, is_orphan = FALSE, disposition_code = 'renewed'
                    WHERE event_id = 'evt_08';
                    """,
                    (lead_id,),
                )
        conn.rollback()


# ---------------------------------------------------------------------------
# IT-09: Attempt to modify event payload while linking
# ---------------------------------------------------------------------------
def test_it_09_attempt_to_modify_event_payload_while_linking():
    with get_connection() as conn:
        _seed_base_data(conn)
        lead_id = _create_lead(conn, workflow_id="motor_renewal", phone_bidx="phone_bidx_09")

        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO interaction_ledger (
                    event_id, event_type, workflow_id, call_id, phone_bidx,
                    occurred_at, payload_json, is_orphan, lead_id, drop_id
                ) VALUES (
                    'evt_09', 'call.initiated', 'motor_renewal', 'call_09', 'phone_bidx_09',
                    NOW(), '{"original": true}'::jsonb, TRUE, NULL, 'drop_test_1'
                );
                """
            )
        conn.commit()

        with pytest.raises(psycopg2.Error, match="Immutable ledger violation"):
            with get_cursor(conn) as cur:
                cur.execute(
                    """
                    UPDATE interaction_ledger
                    SET lead_id = %s, is_orphan = FALSE, payload_json = '{"tampered": true}'::jsonb
                    WHERE event_id = 'evt_09';
                    """,
                    (lead_id,),
                )
        conn.rollback()


# ---------------------------------------------------------------------------
# IT-10: Attempt to revert linked event to orphan
# ---------------------------------------------------------------------------
def test_it_10_attempt_to_revert_linked_event_to_orphan():
    with get_connection() as conn:
        _seed_base_data(conn)
        lead_id = _create_lead(conn, workflow_id="motor_renewal", phone_bidx="phone_bidx_10")

        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO interaction_ledger (
                    event_id, event_type, workflow_id, call_id, phone_bidx,
                    occurred_at, payload_json, is_orphan, lead_id, drop_id
                ) VALUES (
                    'evt_10', 'call.initiated', 'motor_renewal', 'call_10', 'phone_bidx_10',
                    NOW(), '{}'::jsonb, FALSE, %s, 'drop_test_1'
                );
                """,
                (lead_id,),
            )
        conn.commit()

        with pytest.raises(psycopg2.Error, match="Already-linked events are permanently immutable"):
            with get_cursor(conn) as cur:
                cur.execute(
                    "UPDATE interaction_ledger SET is_orphan = TRUE, lead_id = NULL WHERE event_id = 'evt_10';"
                )
        conn.rollback()


# ---------------------------------------------------------------------------
# IT-11: Foreign-key violation for invalid disposition
# ---------------------------------------------------------------------------
def test_it_11_foreign_key_violation_for_invalid_disposition():
    with get_connection() as conn:
        _seed_base_data(conn)

        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            with get_cursor(conn) as cur:
                cur.execute(
                    """
                    INSERT INTO interaction_ledger (
                        event_id, event_type, workflow_id, call_id, disposition_code,
                        occurred_at, payload_json, drop_id
                    ) VALUES (
                        'evt_11', 'disposition.set', 'motor_renewal', 'call_11', 'nonexistent_disposition',
                        NOW(), '{}'::jsonb, 'drop_test_1'
                    );
                    """
                )
        conn.rollback()


# ---------------------------------------------------------------------------
# IT-12: Unique lead identity constraint
# ---------------------------------------------------------------------------
def test_it_12_unique_lead_identity_constraint():
    with get_connection() as conn:
        _seed_base_data(conn)
        _create_lead(conn, workflow_id="motor_renewal", phone_bidx="duplicate_bidx")
        conn.commit()

        # Attempt to insert identical (workflow_id, phone_bidx) without ON CONFLICT
        with pytest.raises(psycopg2.errors.UniqueViolation):
            with get_cursor(conn) as cur:
                cur.execute(
                    """
                    INSERT INTO leads (
                        workflow_id, phone_bidx, phone_encrypted, customer_name_encrypted,
                        external_ref, batch_id
                    ) VALUES (
                        'motor_renewal', 'duplicate_bidx', 'enc_phone', 'enc_name', 'REF2', 'batch_002'
                    );
                    """
                )
        conn.rollback()


# ---------------------------------------------------------------------------
# IT-13: Transaction rollback after simulated failure
# ---------------------------------------------------------------------------
def test_it_13_transaction_rollback_after_simulated_failure():
    with get_connection() as conn:
        _seed_base_data(conn)
        conn.commit()

    with pytest.raises(ZeroDivisionError):
        with transaction() as conn:
            with get_cursor(conn) as cur:
                cur.execute(
                    """
                    INSERT INTO ingestion_file_checkpoints (
                        drop_id, file_type, file_path, file_name, file_hash, status, records_count
                    ) VALUES (
                        'drop_test_1', 'LEAD_CSV', 'data/drop_1/leads/b1.csv', 'b1.csv',
                        'abc123hash', 'PROCESSED', 10
                    );
                    """
                )
                cur.execute(
                    """
                    INSERT INTO leads (
                        workflow_id, phone_bidx, phone_encrypted, customer_name_encrypted,
                        external_ref, batch_id
                    ) VALUES (
                        'motor_renewal', 'rollback_test_phone', 'enc_phone', 'enc_name', 'REF3', 'batch_001'
                    );
                    """
                )
                # Simulate mid-transaction crash
                _ = 1 / 0

    # Verify zero dirty writes in database
    with get_connection() as conn:
        with get_cursor(conn) as cur:
            cur.execute("SELECT COUNT(*) FROM leads WHERE phone_bidx = 'rollback_test_phone';")
            assert cur.fetchone()[0] == 0

            cur.execute("SELECT COUNT(*) FROM ingestion_file_checkpoints WHERE file_name = 'b1.csv';")
            assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# IT-14: Override target lead differs from unique call-associated lead
# ---------------------------------------------------------------------------
def test_it_14_override_target_lead_differs_from_unique_call_associated_lead():
    with get_connection() as conn:
        _seed_base_data(conn)
        lead_X = _create_lead(conn, workflow_id="motor_renewal", phone_bidx="phone_lead_X")
        lead_Y = _create_lead(conn, workflow_id="motor_renewal", phone_bidx="phone_lead_Y")

        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO interaction_ledger (
                    event_id, event_type, workflow_id, call_id, phone_bidx,
                    occurred_at, payload_json, is_orphan, lead_id, drop_id
                ) VALUES (
                    'evt_call_14', 'call.initiated', 'motor_renewal', 'call_14', 'phone_lead_X',
                    NOW(), '{}'::jsonb, FALSE, %s, 'drop_test_1'
                );
                """,
                (lead_X,),
            )

            cur.execute(
                """
                INSERT INTO interaction_ledger (
                    event_id, event_type, workflow_id, call_id, phone_bidx,
                    disposition_code, occurred_at, payload_json, is_orphan, lead_id, drop_id
                ) VALUES (
                    'evt_ovr_14', 'disposition.override', 'motor_renewal', 'call_14', NULL,
                    'renewed', NOW(), '{}'::jsonb, TRUE, NULL, 'drop_test_1'
                );
                """
            )
        conn.commit()

        # Attempt to link override to lead_Y (target differs from call-associated lead_X)
        with pytest.raises(psycopg2.Error, match="Override association mismatch"):
            with get_cursor(conn) as cur:
                cur.execute(
                    "UPDATE interaction_ledger SET lead_id = %s, is_orphan = FALSE WHERE event_id = 'evt_ovr_14';",
                    (lead_Y,),
                )
        conn.rollback()


# ---------------------------------------------------------------------------
# IT-15: DELETE against interaction_ledger is rejected
# ---------------------------------------------------------------------------
def test_it_15_delete_against_interaction_ledger_rejected():
    with get_connection() as conn:
        _seed_base_data(conn)
        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO interaction_ledger (
                    event_id, event_type, workflow_id, call_id, phone_bidx,
                    occurred_at, payload_json, is_orphan, lead_id, drop_id
                ) VALUES (
                    'evt_15', 'call.initiated', 'motor_renewal', 'call_15', 'phone_bidx_15',
                    NOW(), '{}'::jsonb, TRUE, NULL, 'drop_test_1'
                );
                """
            )
        conn.commit()

        # Attempt deletion
        with pytest.raises(psycopg2.Error, match="Append-only ledger violation: Deletion of records"):
            with get_cursor(conn) as cur:
                cur.execute("DELETE FROM interaction_ledger WHERE event_id = 'evt_15';")
        conn.rollback()


# ---------------------------------------------------------------------------
# IT-16: TRUNCATE against interaction_ledger is rejected
# ---------------------------------------------------------------------------
def test_it_16_truncate_against_interaction_ledger_rejected():
    with get_connection() as conn:
        with pytest.raises(psycopg2.Error, match="Append-only ledger violation: TRUNCATE"):
            with get_cursor(conn) as cur:
                cur.execute("TRUNCATE TABLE interaction_ledger;")
        conn.rollback()
