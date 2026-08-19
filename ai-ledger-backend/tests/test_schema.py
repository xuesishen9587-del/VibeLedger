import os
os.environ["ENVIRONMENT"] = "test"
os.environ["DB_SCHEMA"] = "vibeledger_test_runner"

import unittest
import uuid
from decimal import Decimal
from datetime import date, datetime
from typing import Dict, Any, List
import psycopg2
from psycopg2 import sql
from app import config
from app.db import get_connection, transaction
from migrations import runner
from app.repositories import accounts, ingestion, audit

# Complete deterministic schema contract derived from PHYSICAL_SCHEMA.md
EXPECTED_TABLE_CONTRACTS = {
    "households": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "name": {"type": "text", "nullable": "NO"},
            "reporting_currency": {"type": "character", "length": 3, "nullable": "NO"},
            "ledger_start_date": {"type": "date", "nullable": "NO"},
            "status": {"type": "text", "nullable": "NO"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "updated_at": {"type": "timestamp with time zone", "nullable": "NO"},
        },
        "pk": ["id"],
    },
    "users": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "auth_subject": {"type": "text", "nullable": "NO"},
            "email": {"type": "USER-DEFINED", "nullable": "YES"},
            "display_name": {"type": "text", "nullable": "NO"},
            "default_currency": {"type": "character", "length": 3, "nullable": "NO"},
            "status": {"type": "text", "nullable": "NO"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "updated_at": {"type": "timestamp with time zone", "nullable": "NO"},
        },
        "pk": ["id"],
    },
    "household_members": {
        "columns": {
            "household_id": {"type": "uuid", "nullable": "NO"},
            "user_id": {"type": "uuid", "nullable": "NO"},
            "role": {"type": "text", "nullable": "NO"},
            "joined_at": {"type": "timestamp with time zone", "nullable": "NO"},
        },
        "pk": ["household_id", "user_id"],
    },
    "devices": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "user_id": {"type": "uuid", "nullable": "NO"},
            "device_name": {"type": "text", "nullable": "NO"},
            "platform": {"type": "text", "nullable": "NO"},
            "token_hash": {"type": "bytea", "nullable": "NO"},
            "status": {"type": "text", "nullable": "NO"},
            "client_version": {"type": "text", "nullable": "YES"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "last_seen_at": {"type": "timestamp with time zone", "nullable": "YES"},
            "revoked_at": {"type": "timestamp with time zone", "nullable": "YES"},
        },
        "pk": ["id"],
    },
    "accounts": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "household_id": {"type": "uuid", "nullable": "NO"},
            "name": {"type": "text", "nullable": "NO"},
            "institution": {"type": "text", "nullable": "YES"},
            "account_type": {"type": "text", "nullable": "NO"},
            "currency": {"type": "character", "length": 3, "nullable": "NO"},
            "owner_user_id": {"type": "uuid", "nullable": "YES"},
            "linked_cash_account_id": {"type": "uuid", "nullable": "YES"},
            "billing_day": {"type": "smallint", "nullable": "YES"},
            "due_day": {"type": "smallint", "nullable": "YES"},
            "status": {"type": "text", "nullable": "NO"},
            "row_version": {"type": "bigint", "nullable": "NO"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "updated_at": {"type": "timestamp with time zone", "nullable": "NO"},
        },
        "pk": ["id"],
    },
    "account_state": {
        "columns": {
            "account_id": {"type": "uuid", "nullable": "NO"},
            "ledger_balance": {"type": "numeric", "precision": 20, "scale": 6, "nullable": "NO"},
            "initialized_at": {"type": "timestamp with time zone", "nullable": "YES"},
            "last_transaction_at": {"type": "timestamp with time zone", "nullable": "YES"},
            "last_authoritative_snapshot_at": {"type": "timestamp with time zone", "nullable": "YES"},
            "row_version": {"type": "bigint", "nullable": "NO"},
            "updated_at": {"type": "timestamp with time zone", "nullable": "NO"},
        },
        "pk": ["account_id"],
    },
    "account_aliases": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "account_id": {"type": "uuid", "nullable": "NO"},
            "alias_text": {"type": "text", "nullable": "NO"},
            "normalized_alias": {"type": "text", "nullable": "NO"},
            "status": {"type": "text", "nullable": "NO"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "deleted_at": {"type": "timestamp with time zone", "nullable": "YES"},
        },
        "pk": ["id"],
    },
    "categories": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "household_id": {"type": "uuid", "nullable": "NO"},
            "name": {"type": "text", "nullable": "NO"},
            "category_type": {"type": "text", "nullable": "NO"},
            "status": {"type": "text", "nullable": "NO"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "updated_at": {"type": "timestamp with time zone", "nullable": "NO"},
        },
        "pk": ["id"],
    },
    "ingestion_requests": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "device_id": {"type": "uuid", "nullable": "NO"},
            "idempotency_key": {"type": "text", "nullable": "NO"},
            "request_kind": {"type": "text", "nullable": "NO"},
            "request_hash": {"type": "bytea", "nullable": "NO"},
            "status": {"type": "text", "nullable": "NO"},
            "captured_at": {"type": "timestamp with time zone", "nullable": "YES"},
            "client_version": {"type": "text", "nullable": "YES"},
            "draft_payload": {"type": "jsonb", "nullable": "YES"},
            "response_payload": {"type": "jsonb", "nullable": "YES"},
            "failure_code": {"type": "text", "nullable": "YES"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "updated_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "committed_at": {"type": "timestamp with time zone", "nullable": "YES"},
        },
        "pk": ["id"],
    },
    "transactions": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "household_id": {"type": "uuid", "nullable": "NO"},
            "transaction_type": {"type": "text", "nullable": "NO"},
            "occurred_on": {"type": "date", "nullable": "NO"},
            "occurred_at": {"type": "timestamp with time zone", "nullable": "YES"},
            "posted_on": {"type": "date", "nullable": "YES"},
            "from_account_id": {"type": "uuid", "nullable": "YES"},
            "to_account_id": {"type": "uuid", "nullable": "YES"},
            "original_amount": {"type": "numeric", "precision": 20, "scale": 6, "nullable": "NO"},
            "original_currency": {"type": "character", "length": 3, "nullable": "NO"},
            "from_amount": {"type": "numeric", "precision": 20, "scale": 6, "nullable": "YES"},
            "from_currency": {"type": "character", "length": 3, "nullable": "YES"},
            "to_amount": {"type": "numeric", "precision": 20, "scale": 6, "nullable": "YES"},
            "to_currency": {"type": "character", "length": 3, "nullable": "YES"},
            "effective_fx_rate": {"type": "numeric", "precision": 24, "scale": 12, "nullable": "YES"},
            "account_leg_status": {"type": "text", "nullable": "YES"},
            "reporting_amount": {"type": "numeric", "precision": 20, "scale": 6, "nullable": "YES"},
            "reporting_currency": {"type": "character", "length": 3, "nullable": "YES"},
            "reporting_fx_rate": {"type": "numeric", "precision": 24, "scale": 12, "nullable": "YES"},
            "reporting_fx_locked_at": {"type": "timestamp with time zone", "nullable": "YES"},
            "category_id": {"type": "uuid", "nullable": "YES"},
            "merchant": {"type": "text", "nullable": "YES"},
            "merchant_normalized": {"type": "text", "nullable": "YES"},
            "remarks": {"type": "text", "nullable": "YES"},
            "source": {"type": "text", "nullable": "NO"},
            "status": {"type": "text", "nullable": "NO"},
            "verification_status": {"type": "text", "nullable": "NO"},
            "confidence": {"type": "numeric", "precision": 5, "scale": 4, "nullable": "YES"},
            "source_request_id": {"type": "uuid", "nullable": "YES"},
            "statement_batch_id": {"type": "uuid", "nullable": "YES"},
            "created_by_user_id": {"type": "uuid", "nullable": "YES"},
            "created_by_device_id": {"type": "uuid", "nullable": "YES"},
            "row_version": {"type": "bigint", "nullable": "NO"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "updated_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "deleted_at": {"type": "timestamp with time zone", "nullable": "YES"},
            "deleted_by_user_id": {"type": "uuid", "nullable": "YES"},
            "delete_reason": {"type": "text", "nullable": "YES"},
        },
        "pk": ["id"],
    },
    "transaction_links": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "source_transaction_id": {"type": "uuid", "nullable": "NO"},
            "target_transaction_id": {"type": "uuid", "nullable": "NO"},
            "relation_type": {"type": "text", "nullable": "NO"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
        },
        "pk": ["id"],
    },
    "account_snapshots": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "household_id": {"type": "uuid", "nullable": "NO"},
            "account_id": {"type": "uuid", "nullable": "NO"},
            "as_of": {"type": "timestamp with time zone", "nullable": "NO"},
            "balance": {"type": "numeric", "precision": 20, "scale": 6, "nullable": "NO"},
            "currency": {"type": "character", "length": 3, "nullable": "NO"},
            "snapshot_type": {"type": "text", "nullable": "NO"},
            "source": {"type": "text", "nullable": "NO"},
            "reconciliation_batch_id": {"type": "uuid", "nullable": "YES"},
            "source_request_id": {"type": "uuid", "nullable": "YES"},
            "is_authoritative": {"type": "boolean", "nullable": "NO"},
            "created_by_user_id": {"type": "uuid", "nullable": "YES"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
        },
        "pk": ["id"],
    },
    "credit_card_snapshots": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "household_id": {"type": "uuid", "nullable": "NO"},
            "account_id": {"type": "uuid", "nullable": "NO"},
            "as_of": {"type": "timestamp with time zone", "nullable": "NO"},
            "statement_period_start": {"type": "date", "nullable": "YES"},
            "statement_period_end": {"type": "date", "nullable": "YES"},
            "statement_balance": {"type": "numeric", "precision": 20, "scale": 6, "nullable": "YES"},
            "remaining_statement_due": {"type": "numeric", "precision": 20, "scale": 6, "nullable": "YES"},
            "unbilled_balance": {"type": "numeric", "precision": 20, "scale": 6, "nullable": "YES"},
            "current_outstanding": {"type": "numeric", "precision": 20, "scale": 6, "nullable": "YES"},
            "currency": {"type": "character", "length": 3, "nullable": "NO"},
            "source": {"type": "text", "nullable": "NO"},
            "reconciliation_batch_id": {"type": "uuid", "nullable": "YES"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
        },
        "pk": ["id"],
    },
    "investment_pnl_periods": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "household_id": {"type": "uuid", "nullable": "NO"},
            "account_id": {"type": "uuid", "nullable": "NO"},
            "opening_snapshot_id": {"type": "uuid", "nullable": "NO"},
            "closing_snapshot_id": {"type": "uuid", "nullable": "NO"},
            "period_start": {"type": "timestamp with time zone", "nullable": "NO"},
            "period_end": {"type": "timestamp with time zone", "nullable": "NO"},
            "contributions_amount": {"type": "numeric", "precision": 20, "scale": 6, "nullable": "NO"},
            "withdrawals_amount": {"type": "numeric", "precision": 20, "scale": 6, "nullable": "NO"},
            "pnl_amount": {"type": "numeric", "precision": 20, "scale": 6, "nullable": "NO"},
            "currency": {"type": "character", "length": 3, "nullable": "NO"},
            "status": {"type": "text", "nullable": "NO"},
            "calculation_version": {"type": "integer", "nullable": "NO"},
            "reconciliation_batch_id": {"type": "uuid", "nullable": "YES"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "updated_at": {"type": "timestamp with time zone", "nullable": "NO"},
        },
        "pk": ["id"],
    },
    "installment_plans": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "household_id": {"type": "uuid", "nullable": "NO"},
            "credit_account_id": {"type": "uuid", "nullable": "NO"},
            "purchase_occurred_on": {"type": "date", "nullable": "NO"},
            "merchant": {"type": "text", "nullable": "YES"},
            "original_amount": {"type": "numeric", "precision": 20, "scale": 6, "nullable": "NO"},
            "original_currency": {"type": "character", "length": 3, "nullable": "NO"},
            "account_principal_amount": {"type": "numeric", "precision": 20, "scale": 6, "nullable": "YES"},
            "account_currency": {"type": "character", "length": 3, "nullable": "NO"},
            "total_periods": {"type": "smallint", "nullable": "NO"},
            "first_statement_month": {"type": "date", "nullable": "YES"},
            "status": {"type": "text", "nullable": "NO"},
            "source_request_id": {"type": "uuid", "nullable": "YES"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "updated_at": {"type": "timestamp with time zone", "nullable": "NO"},
        },
        "pk": ["id"],
    },
    "installment_periods": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "plan_id": {"type": "uuid", "nullable": "NO"},
            "period_no": {"type": "smallint", "nullable": "NO"},
            "recognition_month": {"type": "date", "nullable": "YES"},
            "scheduled_amount": {"type": "numeric", "precision": 20, "scale": 6, "nullable": "NO"},
            "currency": {"type": "character", "length": 3, "nullable": "NO"},
            "status": {"type": "text", "nullable": "NO"},
            "statement_line_id": {"type": "uuid", "nullable": "YES"},
            "expense_transaction_id": {"type": "uuid", "nullable": "YES"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "updated_at": {"type": "timestamp with time zone", "nullable": "NO"},
        },
        "pk": ["id"],
    },
    "reconciliation_batches": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "household_id": {"type": "uuid", "nullable": "NO"},
            "account_id": {"type": "uuid", "nullable": "NO"},
            "batch_type": {"type": "text", "nullable": "NO"},
            "period_start": {"type": "date", "nullable": "YES"},
            "period_end": {"type": "date", "nullable": "YES"},
            "status": {"type": "text", "nullable": "NO"},
            "currency": {"type": "character", "length": 3, "nullable": "NO"},
            "authoritative_balance": {"type": "numeric", "precision": 20, "scale": 6, "nullable": "YES"},
            "statement_balance": {"type": "numeric", "precision": 20, "scale": 6, "nullable": "YES"},
            "current_outstanding": {"type": "numeric", "precision": 20, "scale": 6, "nullable": "YES"},
            "unbilled_balance": {"type": "numeric", "precision": 20, "scale": 6, "nullable": "YES"},
            "residual_amount": {"type": "numeric", "precision": 20, "scale": 6, "nullable": "YES"},
            "adjustment_amount": {"type": "numeric", "precision": 20, "scale": 6, "nullable": "YES"},
            "matched_count": {"type": "integer", "nullable": "NO"},
            "created_count": {"type": "integer", "nullable": "NO"},
            "pending_count": {"type": "integer", "nullable": "NO"},
            "parser_version": {"type": "text", "nullable": "YES"},
            "engine_version": {"type": "text", "nullable": "NO"},
            "source_request_id": {"type": "uuid", "nullable": "YES"},
            "created_by_user_id": {"type": "uuid", "nullable": "YES"},
            "row_version": {"type": "bigint", "nullable": "NO"},
            "failure_code": {"type": "text", "nullable": "YES"},
            "failure_detail": {"type": "text", "nullable": "YES"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "updated_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "committed_at": {"type": "timestamp with time zone", "nullable": "YES"},
        },
        "pk": ["id"],
    },
    "statement_lines": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "batch_id": {"type": "uuid", "nullable": "NO"},
            "source_page_no": {"type": "integer", "nullable": "YES"},
            "source_row_no": {"type": "integer", "nullable": "YES"},
            "transaction_on": {"type": "date", "nullable": "YES"},
            "posted_on": {"type": "date", "nullable": "YES"},
            "description_raw": {"type": "text", "nullable": "NO"},
            "description_normalized": {"type": "text", "nullable": "YES"},
            "amount": {"type": "numeric", "precision": 20, "scale": 6, "nullable": "NO"},
            "currency": {"type": "character", "length": 3, "nullable": "NO"},
            "direction": {"type": "text", "nullable": "NO"},
            "line_type": {"type": "text", "nullable": "NO"},
            "match_status": {"type": "text", "nullable": "NO"},
            "matched_transaction_id": {"type": "uuid", "nullable": "YES"},
            "confidence": {"type": "numeric", "precision": 5, "scale": 4, "nullable": "YES"},
            "line_fingerprint": {"type": "bytea", "nullable": "YES"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
        },
        "pk": ["id"],
    },
    "reconciliation_candidates": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "batch_id": {"type": "uuid", "nullable": "NO"},
            "statement_line_id": {"type": "uuid", "nullable": "YES"},
            "candidate_type": {"type": "text", "nullable": "NO"},
            "status": {"type": "text", "nullable": "NO"},
            "target_transaction_id": {"type": "uuid", "nullable": "YES"},
            "payload": {"type": "jsonb", "nullable": "NO"},
            "confidence": {"type": "numeric", "precision": 5, "scale": 4, "nullable": "YES"},
            "reason_code": {"type": "text", "nullable": "YES"},
            "reason_detail": {"type": "text", "nullable": "YES"},
            "resolved_by_user_id": {"type": "uuid", "nullable": "YES"},
            "resolved_at": {"type": "timestamp with time zone", "nullable": "YES"},
            "applied_transaction_id": {"type": "uuid", "nullable": "YES"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "updated_at": {"type": "timestamp with time zone", "nullable": "NO"},
        },
        "pk": ["id"],
    },
    "audit_events": {
        "columns": {
            "id": {"type": "bigint", "nullable": "NO"},
            "household_id": {"type": "uuid", "nullable": "NO"},
            "actor_type": {"type": "text", "nullable": "NO"},
            "actor_user_id": {"type": "uuid", "nullable": "YES"},
            "actor_device_id": {"type": "uuid", "nullable": "YES"},
            "request_id": {"type": "uuid", "nullable": "YES"},
            "reconciliation_batch_id": {"type": "uuid", "nullable": "YES"},
            "entity_type": {"type": "text", "nullable": "NO"},
            "entity_id": {"type": "uuid", "nullable": "NO"},
            "action": {"type": "text", "nullable": "NO"},
            "before_data": {"type": "jsonb", "nullable": "YES"},
            "after_data": {"type": "jsonb", "nullable": "YES"},
            "metadata": {"type": "jsonb", "nullable": "YES"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
        },
        "pk": ["id"],
    },
}

class TestSchemaAndRepository(unittest.TestCase):
    def setUp(self):
        if not config.is_safe_for_testing():
            self.skipTest("Skipping integration test. ENVIRONMENT must be 'test'.")
            
        self.test_schema = f"vibeledger_test_{uuid.uuid4().hex[:12]}"
        runner.run_migrations(self.test_schema)
        self.conn = get_connection(self.test_schema)
        
    def tearDown(self):
        if hasattr(self, "conn") and self.conn:
            self.conn.close()
            
        if config.is_safe_for_testing() and hasattr(self, "test_schema"):
            config.validate_test_schema(self.test_schema)
            settings = config.get_settings()
            conn = get_connection(settings.DB_SCHEMA)
            try:
                with conn.cursor() as cur:
                    quoted_schema = sql.Identifier(self.test_schema)
                    cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {schema} CASCADE").format(schema=quoted_schema))
                conn.commit()
            except Exception as e:
                print(f"Warning: failed to drop test schema {self.test_schema}: {e}")
            finally:
                conn.close()

    # --- 1. Exhaustive Data-Driven Schema Parity Check ---
    def test_exhaustive_schema_parity(self):
        """
        Validates all 20 business target tables against the expected schema contract:
        - Complete column set matching
        - Exact data types
        - Character lengths (CHAR(3))
        - Numeric precision & scale (20,6 / 24,12 / 5,4)
        - Nullability
        - Primary key constraints
        """
        with self.conn.cursor() as cur:
            # Query all table names in test schema
            cur.execute("""
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_schema = %s AND table_type = 'BASE TABLE';
            """, (self.test_schema,))
            actual_tables = {row[0] for row in cur.fetchall()} - {"schema_migrations"}
            
            # Verify all expected tables exist and no extra tables exist
            self.assertEqual(
                actual_tables,
                set(EXPECTED_TABLE_CONTRACTS.keys()),
                f"Table set mismatch: diff={actual_tables.symmetric_difference(EXPECTED_TABLE_CONTRACTS.keys())}"
            )
            
            for table_name, contract in EXPECTED_TABLE_CONTRACTS.items():
                # Fetch actual columns
                cur.execute("""
                    SELECT column_name, data_type, character_maximum_length, 
                           numeric_precision, numeric_scale, is_nullable
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s;
                """, (self.test_schema, table_name))
                columns = {row[0]: {
                    "type": row[1],
                    "length": row[2],
                    "precision": row[3],
                    "scale": row[4],
                    "nullable": row[5]
                } for row in cur.fetchall()}
                
                expected_cols = contract["columns"]
                self.assertEqual(
                    set(columns.keys()),
                    set(expected_cols.keys()),
                    f"Column set mismatch in table '{table_name}'"
                )
                
                for col_name, exp_meta in expected_cols.items():
                    act_meta = columns[col_name]
                    self.assertEqual(
                        act_meta["type"], exp_meta["type"],
                        f"Data type mismatch for {table_name}.{col_name}: act={act_meta['type']}, exp={exp_meta['type']}"
                    )
                    self.assertEqual(
                        act_meta["nullable"], exp_meta["nullable"],
                        f"Nullability mismatch for {table_name}.{col_name}: act={act_meta['nullable']}, exp={exp_meta['nullable']}"
                    )
                    if "length" in exp_meta:
                        self.assertEqual(
                            act_meta["length"], exp_meta["length"],
                            f"Length mismatch for {table_name}.{col_name}: act={act_meta['length']}, exp={exp_meta['length']}"
                        )
                    if "precision" in exp_meta:
                        self.assertEqual(
                            act_meta["precision"], exp_meta["precision"],
                            f"Precision mismatch for {table_name}.{col_name}: act={act_meta['precision']}, exp={exp_meta['precision']}"
                        )
                    if "scale" in exp_meta:
                        self.assertEqual(
                            act_meta["scale"], exp_meta["scale"],
                            f"Scale mismatch for {table_name}.{col_name}: act={act_meta['scale']}, exp={exp_meta['scale']}"
                        )

                # Verify PK
                cur.execute("""
                    SELECT kcu.column_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                    WHERE tc.table_schema = %s
                      AND tc.table_name = %s
                      AND tc.constraint_type = 'PRIMARY KEY'
                    ORDER BY kcu.ordinal_position;
                """, (self.test_schema, table_name))
                actual_pk = [row[0] for row in cur.fetchall()]
                self.assertEqual(
                    actual_pk, contract["pk"],
                    f"PK mismatch for table '{table_name}': act={actual_pk}, exp={contract['pk']}"
                )

    # --- 2. Required NOT NULL & Enum Column Rejection Tests ---
    def test_required_not_null_column_rejections(self):
        h_id = uuid.uuid4()
        u_id = uuid.uuid4()
        d_id = uuid.uuid4()
        accounts.create_household(self.conn, h_id, "HH_NotNull", date(2026, 1, 1))
        accounts.create_user(self.conn, u_id, "auth_not_null", "User_NotNull")
        accounts.create_device(self.conn, d_id, u_id, "Dev_NotNull", "ios_shortcuts", b"hash_notnull")
        self.conn.commit()

        test_null_inserts = [
            ("households", "INSERT INTO households (id, name, ledger_start_date, status) VALUES (%s, %s, %s, NULL);", (uuid.uuid4(), "H", date(2026, 1, 1))),
            ("users", "INSERT INTO users (id, auth_subject, display_name, status) VALUES (%s, %s, %s, NULL);", (uuid.uuid4(), "sub1", "U")),
            ("household_members", "INSERT INTO household_members (household_id, user_id, role) VALUES (%s, %s, NULL);", (h_id, u_id)),
            ("devices", "INSERT INTO devices (id, user_id, device_name, platform, token_hash, status) VALUES (%s, %s, %s, %s, %s, NULL);", (uuid.uuid4(), u_id, "D", "ios_shortcuts", b"t1")),
            ("devices", "INSERT INTO devices (id, user_id, device_name, platform, token_hash, status) VALUES (%s, %s, %s, NULL, %s, %s);", (uuid.uuid4(), u_id, "D", b"t2", "active")),
            ("accounts", "INSERT INTO accounts (id, household_id, name, account_type, currency, status) VALUES (%s, %s, %s, NULL, %s, %s);", (uuid.uuid4(), h_id, "A1", "CNY", "active")),
            ("accounts", "INSERT INTO accounts (id, household_id, name, account_type, currency, status) VALUES (%s, %s, %s, %s, %s, NULL);", (uuid.uuid4(), h_id, "A2", "cash", "CNY")),
            ("categories", "INSERT INTO categories (id, household_id, name, category_type, status) VALUES (%s, %s, %s, NULL, %s);", (uuid.uuid4(), h_id, "C1", "active")),
            ("categories", "INSERT INTO categories (id, household_id, name, category_type, status) VALUES (%s, %s, %s, %s, NULL);", (uuid.uuid4(), h_id, "C2", "expense")),
            ("ingestion_requests", "INSERT INTO ingestion_requests (id, device_id, idempotency_key, request_kind, request_hash, status) VALUES (%s, %s, %s, NULL, %s, %s);", (uuid.uuid4(), d_id, "key1234567", b"h1", "received")),
            ("ingestion_requests", "INSERT INTO ingestion_requests (id, device_id, idempotency_key, request_kind, request_hash, status) VALUES (%s, %s, %s, %s, %s, NULL);", (uuid.uuid4(), d_id, "key1234568", "expense", b"h2")),
            ("transactions", "INSERT INTO transactions (id, household_id, transaction_type, occurred_on, original_amount, original_currency, source, status) VALUES (%s, %s, NULL, %s, %s, %s, %s, %s);", (uuid.uuid4(), h_id, date(2026, 1, 1), 10.0, "CNY", "shortcut", "committed")),
            ("transactions", "INSERT INTO transactions (id, household_id, transaction_type, occurred_on, original_amount, original_currency, source, status) VALUES (%s, %s, %s, %s, %s, %s, NULL, %s);", (uuid.uuid4(), h_id, "expense", date(2026, 1, 1), 10.0, "CNY", "committed")),
            ("transactions", "INSERT INTO transactions (id, household_id, transaction_type, occurred_on, original_amount, original_currency, source, status) VALUES (%s, %s, %s, %s, %s, %s, %s, NULL);", (uuid.uuid4(), h_id, "expense", date(2026, 1, 1), 10.0, "CNY", "shortcut")),
            ("installment_plans", "INSERT INTO installment_plans (id, household_id, credit_account_id, purchase_occurred_on, original_amount, original_currency, account_currency, total_periods, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL);", (uuid.uuid4(), h_id, uuid.uuid4(), date(2026, 1, 1), 100.0, "CNY", "CNY", 3)),
            ("reconciliation_batches", "INSERT INTO reconciliation_batches (id, household_id, account_id, batch_type, status, currency, engine_version) VALUES (%s, %s, %s, NULL, %s, %s, %s);", (uuid.uuid4(), h_id, uuid.uuid4(), "processing", "CNY", "1")),
            ("reconciliation_batches", "INSERT INTO reconciliation_batches (id, household_id, account_id, batch_type, status, currency, engine_version) VALUES (%s, %s, %s, %s, NULL, %s, %s);", (uuid.uuid4(), h_id, uuid.uuid4(), "statement", "CNY", "1")),
            ("reconciliation_batches", "INSERT INTO reconciliation_batches (id, household_id, account_id, batch_type, status, currency, engine_version) VALUES (%s, %s, %s, %s, %s, %s, NULL);", (uuid.uuid4(), h_id, uuid.uuid4(), "statement", "processing", "CNY")),
            ("statement_lines", "INSERT INTO statement_lines (id, batch_id, description_raw, amount, currency, direction, line_type, match_status) VALUES (%s, %s, %s, %s, %s, NULL, %s, %s);", (uuid.uuid4(), uuid.uuid4(), "desc", 10.0, "CNY", "expense", "unmatched")),
            ("statement_lines", "INSERT INTO statement_lines (id, batch_id, description_raw, amount, currency, direction, line_type, match_status) VALUES (%s, %s, %s, %s, %s, %s, NULL, %s);", (uuid.uuid4(), uuid.uuid4(), "desc", 10.0, "CNY", "debit", "unmatched")),
            ("statement_lines", "INSERT INTO statement_lines (id, batch_id, description_raw, amount, currency, direction, line_type, match_status) VALUES (%s, %s, %s, %s, %s, %s, %s, NULL);", (uuid.uuid4(), uuid.uuid4(), "desc", 10.0, "CNY", "debit", "expense")),
            ("reconciliation_candidates", "INSERT INTO reconciliation_candidates (id, batch_id, candidate_type, status, payload) VALUES (%s, %s, NULL, %s, %s);", (uuid.uuid4(), uuid.uuid4(), "proposed", "{}")),
            ("reconciliation_candidates", "INSERT INTO reconciliation_candidates (id, batch_id, candidate_type, status, payload) VALUES (%s, %s, %s, NULL, %s);", (uuid.uuid4(), uuid.uuid4(), "match", "{}")),
            ("audit_events", "INSERT INTO audit_events (household_id, actor_type, entity_type, entity_id, action) VALUES (%s, NULL, %s, %s, %s);", (h_id, "account", uuid.uuid4(), "create")),
            ("audit_events", "INSERT INTO audit_events (household_id, actor_type, entity_type, entity_id, action) VALUES (%s, %s, %s, %s, NULL);", (h_id, "system", "account", uuid.uuid4())),
        ]

        with self.conn.cursor() as cur:
            for tbl, sql_stmt, params in test_null_inserts:
                with self.assertRaises(psycopg2.IntegrityError, msg=f"Table {tbl} allowed NULL for required field"):
                    cur.execute(sql_stmt, params)
                self.conn.rollback()

    # --- 3. Transaction Lifecycle Constraint Tests ---
    def test_transaction_lifecycle_invariants(self):
        h_id = uuid.uuid4()
        acc_id = uuid.uuid4()
        accounts.create_household(self.conn, h_id, "HH_Tx", date(2026, 1, 1))
        accounts.create_account(self.conn, acc_id, h_id, "Cash_Tx", "cash", "CNY")
        self.conn.commit()

        with self.conn.cursor() as cur:
            # 1. Valid committed transaction succeeds
            tx_committed_id = uuid.uuid4()
            cur.execute("""
                INSERT INTO transactions (
                    id, household_id, transaction_type, occurred_on, from_account_id, 
                    original_amount, original_currency, source, status, verification_status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
            """, (tx_committed_id, h_id, "expense", date(2026, 1, 1), acc_id, 100.0, "CNY", "shortcut", "committed", "unverified"))
            self.conn.commit()

            # 2. Reject: status = 'committed' AND deleted_at IS NOT NULL
            with self.assertRaises(psycopg2.IntegrityError):
                cur.execute("""
                    INSERT INTO transactions (
                        id, household_id, transaction_type, occurred_on, from_account_id, 
                        original_amount, original_currency, source, status, deleted_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now());
                """, (uuid.uuid4(), h_id, "expense", date(2026, 1, 1), acc_id, 100.0, "CNY", "shortcut", "committed"))
            self.conn.rollback()

            # 3. Reject: status = 'committed' AND delete_reason IS NOT NULL
            with self.assertRaises(psycopg2.IntegrityError):
                cur.execute("""
                    INSERT INTO transactions (
                        id, household_id, transaction_type, occurred_on, from_account_id, 
                        original_amount, original_currency, source, status, delete_reason
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """, (uuid.uuid4(), h_id, "expense", date(2026, 1, 1), acc_id, 100.0, "CNY", "shortcut", "committed", "mistake"))
            self.conn.rollback()

            # 4. Reject: status = 'voided' AND deleted_at IS NULL
            with self.assertRaises(psycopg2.IntegrityError):
                cur.execute("""
                    INSERT INTO transactions (
                        id, household_id, transaction_type, occurred_on, from_account_id, 
                        original_amount, original_currency, source, status, delete_reason
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """, (uuid.uuid4(), h_id, "expense", date(2026, 1, 1), acc_id, 100.0, "CNY", "shortcut", "voided", "mistake"))
            self.conn.rollback()

            # 5. Reject: status = 'voided' AND delete_reason IS NULL
            with self.assertRaises(psycopg2.IntegrityError):
                cur.execute("""
                    INSERT INTO transactions (
                        id, household_id, transaction_type, occurred_on, from_account_id, 
                        original_amount, original_currency, source, status, deleted_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now());
                """, (uuid.uuid4(), h_id, "expense", date(2026, 1, 1), acc_id, 100.0, "CNY", "shortcut", "voided"))
            self.conn.rollback()

            # 6. Valid voided transaction succeeds
            tx_voided_id = uuid.uuid4()
            cur.execute("""
                INSERT INTO transactions (
                    id, household_id, transaction_type, occurred_on, from_account_id, 
                    original_amount, original_currency, source, status, deleted_at, delete_reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s);
            """, (tx_voided_id, h_id, "expense", date(2026, 1, 1), acc_id, 50.0, "CNY", "shortcut", "voided", "test void"))
            self.conn.commit()

    # --- 4. Installment Invariants Tests ---
    def test_installment_invariants(self):
        h_id = uuid.uuid4()
        acc_id = uuid.uuid4()
        accounts.create_household(self.conn, h_id, "HH_Inst", date(2026, 1, 1))
        accounts.create_account(self.conn, acc_id, h_id, "Credit_Inst", "credit", "CNY")
        self.conn.commit()

        with self.conn.cursor() as cur:
            # 1. Default status on new plan is 'pending_first_bill'
            plan_id = uuid.uuid4()
            cur.execute("""
                INSERT INTO installment_plans (
                    id, household_id, credit_account_id, purchase_occurred_on, 
                    original_amount, original_currency, account_currency, total_periods
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING status;
            """, (plan_id, h_id, acc_id, date(2026, 1, 1), 600.0, "CNY", "CNY", 6))
            row = cur.fetchone()
            self.assertEqual(row[0], "pending_first_bill")
            self.conn.commit()

            # 2. Reject: total_periods < 2 or > 120
            with self.assertRaises(psycopg2.IntegrityError):
                cur.execute("""
                    INSERT INTO installment_plans (
                        id, household_id, credit_account_id, purchase_occurred_on, 
                        original_amount, original_currency, account_currency, total_periods
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 1);
                """, (uuid.uuid4(), h_id, acc_id, date(2026, 1, 1), 100.0, "CNY", "CNY"))
            self.conn.rollback()

            # 3. Create a transaction for linking
            tx_id = uuid.uuid4()
            cur.execute("""
                INSERT INTO transactions (
                    id, household_id, transaction_type, occurred_on, from_account_id, 
                    original_amount, original_currency, source, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
            """, (tx_id, h_id, "expense", date(2026, 1, 1), acc_id, 100.0, "CNY", "installment", "committed"))
            self.conn.commit()

            # 4. Reject: scheduled period with expense_transaction_id NOT NULL
            with self.assertRaises(psycopg2.IntegrityError):
                cur.execute("""
                    INSERT INTO installment_periods (
                        id, plan_id, period_no, scheduled_amount, currency, status, expense_transaction_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s);
                """, (uuid.uuid4(), plan_id, 1, 100.0, "CNY", "scheduled", tx_id))
            self.conn.rollback()

            # 5. Reject: billed period with expense_transaction_id NULL
            with self.assertRaises(psycopg2.IntegrityError):
                cur.execute("""
                    INSERT INTO installment_periods (
                        id, plan_id, period_no, scheduled_amount, currency, status, expense_transaction_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, NULL);
                """, (uuid.uuid4(), plan_id, 1, 100.0, "CNY", "billed"))
            self.conn.rollback()

            # 6. Valid billed period succeeds
            cur.execute("""
                INSERT INTO installment_periods (
                    id, plan_id, period_no, scheduled_amount, currency, status, expense_transaction_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s);
            """, (uuid.uuid4(), plan_id, 1, 100.0, "CNY", "billed", tx_id))
            self.conn.commit()

    # --- 5. Reconciliation Batch Invariants Tests ---
    def test_reconciliation_batch_invariants(self):
        h_id = uuid.uuid4()
        acc_id = uuid.uuid4()
        accounts.create_household(self.conn, h_id, "HH_Recon", date(2026, 1, 1))
        accounts.create_account(self.conn, acc_id, h_id, "Card_Recon", "credit", "CNY")
        self.conn.commit()

        with self.conn.cursor() as cur:
            # 1. Reject invalid status
            with self.assertRaises(psycopg2.IntegrityError):
                cur.execute("""
                    INSERT INTO reconciliation_batches (
                        id, household_id, account_id, batch_type, status, currency, engine_version
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s);
                """, (uuid.uuid4(), h_id, acc_id, "statement", "unknown_status", "CNY", "1"))
            self.conn.rollback()

            # 2. Reject negative counts
            with self.assertRaises(psycopg2.IntegrityError):
                cur.execute("""
                    INSERT INTO reconciliation_batches (
                        id, household_id, account_id, batch_type, status, currency, engine_version, matched_count
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, -1);
                """, (uuid.uuid4(), h_id, acc_id, "statement", "processing", "CNY", "1"))
            self.conn.rollback()

            # 3. Reject period_end < period_start
            with self.assertRaises(psycopg2.IntegrityError):
                cur.execute("""
                    INSERT INTO reconciliation_batches (
                        id, household_id, account_id, batch_type, status, currency, engine_version, period_start, period_end
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
                """, (uuid.uuid4(), h_id, acc_id, "statement", "processing", "CNY", "1", date(2026, 2, 1), date(2026, 1, 1)))
            self.conn.rollback()

            # 4. Reject committed status without committed_at
            with self.assertRaises(psycopg2.IntegrityError):
                cur.execute("""
                    INSERT INTO reconciliation_batches (
                        id, household_id, account_id, batch_type, status, currency, engine_version, committed_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, NULL);
                """, (uuid.uuid4(), h_id, acc_id, "statement", "committed", "CNY", "1"))
            self.conn.rollback()

            # 5. Valid committed batch succeeds
            cur.execute("""
                INSERT INTO reconciliation_batches (
                    id, household_id, account_id, batch_type, status, currency, engine_version, committed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, now());
            """, (uuid.uuid4(), h_id, acc_id, "statement", "committed", "CNY", "1"))
            self.conn.commit()

    # --- 6. Conservative FK ON DELETE Tests ---
    def test_conservative_foreign_key_semantics(self):
        """
        Durable financial history and evidence must not be deleted when parents are deleted.
        """
        h_id = uuid.uuid4()
        u_id = uuid.uuid4()
        acc_id = uuid.uuid4()
        accounts.create_household(self.conn, h_id, "HH_FK", date(2026, 1, 1))
        accounts.create_user(self.conn, u_id, "auth_fk", "User_FK")
        accounts.create_account(self.conn, acc_id, h_id, "Card_FK", "credit", "CNY")
        self.conn.commit()

        # 1. Attempting to delete account with transactions must fail (RESTRICT)
        tx_id = uuid.uuid4()
        with self.conn.cursor() as cur:
            cur.execute("""
                INSERT INTO transactions (
                    id, household_id, transaction_type, occurred_on, from_account_id, 
                    original_amount, original_currency, source, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
            """, (tx_id, h_id, "expense", date(2026, 1, 1), acc_id, 50.0, "CNY", "shortcut", "committed"))
        self.conn.commit()

        with self.conn.cursor() as cur:
            with self.assertRaises(psycopg2.IntegrityError):
                cur.execute("DELETE FROM accounts WHERE id = %s;", (acc_id,))
        self.conn.rollback()

        # 2. Attempting to delete household with audit events must fail (RESTRICT)
        audit.insert_audit_event(
            self.conn, h_id, "system", "household", h_id, "create"
        )
        self.conn.commit()

        with self.conn.cursor() as cur:
            with self.assertRaises(psycopg2.IntegrityError):
                cur.execute("DELETE FROM households WHERE id = %s;", (h_id,))
        self.conn.rollback()

    # --- 7. Audit Immutability Trigger ---
    def test_audit_event_trigger_immutability(self):
        h_id = uuid.uuid4()
        accounts.create_household(self.conn, h_id, "HH_Audit", date(2026, 1, 1))
        self.conn.commit()
        
        ae_id = uuid.uuid4()
        audit.insert_audit_event(
            self.conn, h_id, "system", "account", ae_id, "create", metadata={"k": "v"}
        )
        self.conn.commit()
        
        events = audit.list_audit_events_for_entity(self.conn, "account", ae_id)
        self.assertEqual(len(events), 1)
        event_db_id = events[0]["id"]
        
        with self.conn.cursor() as cur:
            with self.assertRaises(psycopg2.DatabaseError) as ctx:
                cur.execute("UPDATE audit_events SET entity_type = 'tampered' WHERE id = %s;", (event_db_id,))
            self.assertIn("audit_events is append-only", str(ctx.exception))
            self.conn.rollback()
            
        with self.conn.cursor() as cur:
            with self.assertRaises(psycopg2.DatabaseError) as ctx:
                cur.execute("DELETE FROM audit_events WHERE id = %s;", (event_db_id,))
            self.assertIn("audit_events is append-only", str(ctx.exception))
            self.conn.rollback()

    # --- 8. Concurrency Locking and Atomicity ---
    def test_accounts_atomicity_and_locking(self):
        h_id = uuid.uuid4()
        accounts.create_household(self.conn, h_id, "HH_Atomicity", date(2026, 1, 1))
        acc_id = uuid.uuid4()
        accounts.create_account(self.conn, acc_id, h_id, "Cash_Atomicity", "cash", "CNY")
        self.conn.commit()
        
        # Verify account and state created atomically
        acc = accounts.get_account(self.conn, acc_id)
        self.assertIsNotNone(acc)
        state = accounts.get_account_state(self.conn, acc_id)
        self.assertIsNotNone(state)
        self.assertEqual(state["ledger_balance"], Decimal("0.000000"))
        
        # Test transaction rollback leaves state clean
        try:
            with transaction(self.conn):
                with self.conn.cursor() as cur:
                    cur.execute("UPDATE account_state SET ledger_balance = 99.000000 WHERE account_id = %s;", (acc_id,))
                    locked = accounts.lock_account_state(self.conn, acc_id)
                    self.assertEqual(locked["ledger_balance"], Decimal("99.000000"))
                    raise RuntimeError("Forced Error")
        except RuntimeError:
            pass
            
        state_after = accounts.get_account_state(self.conn, acc_id)
        self.assertEqual(state_after["ledger_balance"], Decimal("0.000000"))
