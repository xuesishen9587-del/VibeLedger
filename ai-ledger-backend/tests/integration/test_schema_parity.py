import unittest
try:
    from tests.support.db_helper import BaseDbTestCase
except ModuleNotFoundError:
    from support.db_helper import BaseDbTestCase

# Canonical 16-table schema contracts derived from 0001_simplified.sql
EXPECTED_TABLE_CONTRACTS = {
    "households": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "name": {"type": "text", "nullable": "NO"},
            "reporting_currency": {"type": "character varying", "length": 3, "nullable": "NO"},
            "started_on": {"type": "date", "nullable": "NO"},
            "timezone": {"type": "text", "nullable": "NO"},
            "investment_review_change_ratio": {"type": "numeric", "nullable": "NO"},
            "status": {"type": "text", "nullable": "NO"},
            "row_version": {"type": "bigint", "nullable": "NO"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "updated_at": {"type": "timestamp with time zone", "nullable": "NO"},
        },
        "pk": ["id"],
    },
    "users": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "auth_subject": {"type": "text", "nullable": "NO"},
            "email": {"type": "text", "nullable": "NO"},
            "display_name": {"type": "text", "nullable": "NO"},
            "status": {"type": "text", "nullable": "NO"},
            "row_version": {"type": "bigint", "nullable": "NO"},
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
            "household_id": {"type": "uuid", "nullable": "NO"},
            "user_id": {"type": "uuid", "nullable": "NO"},
            "name": {"type": "text", "nullable": "NO"},
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
    "categories": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "household_id": {"type": "uuid", "nullable": "NO"},
            "name": {"type": "text", "nullable": "NO"},
            "category_type": {"type": "text", "nullable": "NO"},
            "description": {"type": "text", "nullable": "YES"},
            "is_fallback": {"type": "boolean", "nullable": "NO"},
            "status": {"type": "text", "nullable": "NO"},
            "row_version": {"type": "bigint", "nullable": "NO"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "updated_at": {"type": "timestamp with time zone", "nullable": "NO"},
        },
        "pk": ["id"],
    },
    "accounts": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "household_id": {"type": "uuid", "nullable": "NO"},
            "name": {"type": "text", "nullable": "NO"},
            "balance_scope": {"type": "text", "nullable": "NO"},
            "account_type": {"type": "text", "nullable": "NO"},
            "currency": {"type": "character varying", "length": 3, "nullable": "NO"},
            "owner_user_id": {"type": "uuid", "nullable": "YES"},
            "risk_level": {"type": "text", "nullable": "YES"},
            "opened_on": {"type": "date", "nullable": "NO"},
            "closed_on": {"type": "date", "nullable": "YES"},
            "status": {"type": "text", "nullable": "NO"},
            "statement_import_enabled": {"type": "boolean", "nullable": "NO"},
            "row_version": {"type": "bigint", "nullable": "NO"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "updated_at": {"type": "timestamp with time zone", "nullable": "NO"},
        },
        "pk": ["id"],
    },
    "account_aliases": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "household_id": {"type": "uuid", "nullable": "NO"},
            "account_id": {"type": "uuid", "nullable": "NO"},
            "alias_text": {"type": "text", "nullable": "NO"},
            "normalized_alias": {"type": "text", "nullable": "NO"},
            "status": {"type": "text", "nullable": "NO"},
            "row_version": {"type": "bigint", "nullable": "NO"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "updated_at": {"type": "timestamp with time zone", "nullable": "NO"},
        },
        "pk": ["id"],
    },
    "ingestion_requests": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "household_id": {"type": "uuid", "nullable": "NO"},
            "user_id": {"type": "uuid", "nullable": "NO"},
            "device_id": {"type": "uuid", "nullable": "YES"},
            "actor_scope": {"type": "text", "nullable": "NO"},
            "idempotency_key": {"type": "character varying", "length": 200, "nullable": "NO"},
            "request_kind": {"type": "text", "nullable": "NO"},
            "operation": {"type": "text", "nullable": "NO"},
            "request_hash": {"type": "character varying", "length": 64, "nullable": "YES"},
            "image_sha256": {"type": "character varying", "length": 64, "nullable": "YES"},
            "status": {"type": "text", "nullable": "NO"},
            "captured_at": {"type": "timestamp with time zone", "nullable": "YES"},
            "client_version": {"type": "text", "nullable": "YES"},
            "draft_payload": {"type": "jsonb", "nullable": "YES"},
            "response_payload": {"type": "jsonb", "nullable": "YES"},
            "response_http_status": {"type": "integer", "nullable": "YES"},
            "failure_code": {"type": "text", "nullable": "YES"},
            "row_version": {"type": "bigint", "nullable": "NO"},
            "last_editor_scope": {"type": "text", "nullable": "YES"},
            "statement_account_id": {"type": "uuid", "nullable": "YES"},
            "document_sha256": {"type": "character varying", "length": 64, "nullable": "YES"},
            "period_start": {"type": "date", "nullable": "YES"},
            "period_end": {"type": "date", "nullable": "YES"},
            "parser_version": {"type": "text", "nullable": "YES"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "updated_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "committed_at": {"type": "timestamp with time zone", "nullable": "YES"},
        },
        "pk": ["id"],
    },
    "spending_schedules": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "household_id": {"type": "uuid", "nullable": "NO"},
            "created_by_user_id": {"type": "uuid", "nullable": "NO"},
            "name": {"type": "text", "nullable": "NO"},
            "kind": {"type": "text", "nullable": "NO"},
            "amount_per_period": {"type": "numeric", "nullable": "NO"},
            "currency": {"type": "character varying", "length": 3, "nullable": "NO"},
            "period_count": {"type": "integer", "nullable": "YES"},
            "start_month": {"type": "date", "nullable": "NO"},
            "day_of_month": {"type": "integer", "nullable": "NO"},
            "merchant": {"type": "text", "nullable": "NO"},
            "category_id": {"type": "uuid", "nullable": "NO"},
            "account_id": {"type": "uuid", "nullable": "YES"},
            "status": {"type": "text", "nullable": "NO"},
            "source_request_id": {"type": "uuid", "nullable": "YES"},
            "created_by_device_id": {"type": "uuid", "nullable": "YES"},
            "row_version": {"type": "bigint", "nullable": "NO"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "updated_at": {"type": "timestamp with time zone", "nullable": "NO"},
        },
        "pk": ["id"],
    },
    "schedule_occurrences": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "household_id": {"type": "uuid", "nullable": "NO"},
            "schedule_id": {"type": "uuid", "nullable": "NO"},
            "period_no": {"type": "integer", "nullable": "NO"},
            "due_on": {"type": "date", "nullable": "NO"},
            "amount": {"type": "numeric", "nullable": "NO"},
            "currency": {"type": "character varying", "length": 3, "nullable": "NO"},
            "category_id": {"type": "uuid", "nullable": "NO"},
            "account_id": {"type": "uuid", "nullable": "YES"},
            "status": {"type": "text", "nullable": "NO"},
            "skip_reason": {"type": "text", "nullable": "YES"},
            "row_version": {"type": "bigint", "nullable": "NO"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "updated_at": {"type": "timestamp with time zone", "nullable": "NO"},
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
            "date_source": {"type": "text", "nullable": "NO"},
            "original_amount": {"type": "numeric", "nullable": "NO"},
            "original_currency": {"type": "character varying", "length": 3, "nullable": "NO"},
            "category_id": {"type": "uuid", "nullable": "NO"},
            "source": {"type": "text", "nullable": "NO"},
            "status": {"type": "text", "nullable": "NO"},
            "account_id": {"type": "uuid", "nullable": "YES"},
            "merchant": {"type": "text", "nullable": "YES"},
            "merchant_normalized": {"type": "text", "nullable": "YES"},
            "remarks": {"type": "text", "nullable": "YES"},
            "refund_of_transaction_id": {"type": "uuid", "nullable": "YES"},
            "payment_mode": {"type": "text", "nullable": "YES"},
            "schedule_occurrence_id": {"type": "uuid", "nullable": "YES"},
            "category_uncertain": {"type": "boolean", "nullable": "NO"},
            "account_review_acknowledged": {"type": "boolean", "nullable": "NO"},
            "reporting_amount": {"type": "numeric", "nullable": "YES"},
            "reporting_currency": {"type": "character varying", "length": 3, "nullable": "YES"},
            "reporting_fx_rate": {"type": "numeric", "nullable": "YES"},
            "reporting_fx_as_of": {"type": "date", "nullable": "YES"},
            "reporting_fx_source": {"type": "text", "nullable": "YES"},
            "reporting_fx_locked_at": {"type": "timestamp with time zone", "nullable": "YES"},
            "created_by_user_id": {"type": "uuid", "nullable": "NO"},
            "created_by_device_id": {"type": "uuid", "nullable": "YES"},
            "source_request_id": {"type": "uuid", "nullable": "NO"},
            "source_item_key": {"type": "text", "nullable": "NO"},
            "deleted_at": {"type": "timestamp with time zone", "nullable": "YES"},
            "deleted_by_user_id": {"type": "uuid", "nullable": "YES"},
            "delete_reason": {"type": "text", "nullable": "YES"},
            "row_version": {"type": "bigint", "nullable": "NO"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "updated_at": {"type": "timestamp with time zone", "nullable": "NO"},
        },
        "pk": ["id"],
    },
    "account_snapshots": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "household_id": {"type": "uuid", "nullable": "NO"},
            "account_id": {"type": "uuid", "nullable": "NO"},
            "as_of": {"type": "timestamp with time zone", "nullable": "NO"},
            "time_basis": {"type": "text", "nullable": "NO"},
            "balance": {"type": "numeric", "nullable": "NO"},
            "currency": {"type": "character varying", "length": 3, "nullable": "NO"},
            "source": {"type": "text", "nullable": "NO"},
            "status": {"type": "text", "nullable": "NO"},
            "notes": {"type": "text", "nullable": "YES"},
            "replaces_snapshot_id": {"type": "uuid", "nullable": "YES"},
            "voided_at": {"type": "timestamp with time zone", "nullable": "YES"},
            "voided_by_user_id": {"type": "uuid", "nullable": "YES"},
            "void_reason": {"type": "text", "nullable": "YES"},
            "created_by_user_id": {"type": "uuid", "nullable": "NO"},
            "created_by_device_id": {"type": "uuid", "nullable": "YES"},
            "source_request_id": {"type": "uuid", "nullable": "NO"},
            "row_version": {"type": "bigint", "nullable": "NO"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "updated_at": {"type": "timestamp with time zone", "nullable": "NO"},
        },
        "pk": ["id"],
    },
    "investment_period_inputs": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "household_id": {"type": "uuid", "nullable": "NO"},
            "account_id": {"type": "uuid", "nullable": "NO"},
            "opening_snapshot_id": {"type": "uuid", "nullable": "NO"},
            "closing_snapshot_id": {"type": "uuid", "nullable": "NO"},
            "contributions_amount": {"type": "numeric", "nullable": "NO"},
            "withdrawals_amount": {"type": "numeric", "nullable": "NO"},
            "created_by_user_id": {"type": "uuid", "nullable": "NO"},
            "confirmed_by_user_id": {"type": "uuid", "nullable": "NO"},
            "confirmed_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "notes": {"type": "text", "nullable": "YES"},
            "status": {"type": "text", "nullable": "NO"},
            "voided_at": {"type": "timestamp with time zone", "nullable": "YES"},
            "voided_by_user_id": {"type": "uuid", "nullable": "YES"},
            "void_reason": {"type": "text", "nullable": "YES"},
            "source_request_id": {"type": "uuid", "nullable": "NO"},
            "created_by_device_id": {"type": "uuid", "nullable": "YES"},
            "row_version": {"type": "bigint", "nullable": "NO"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
            "updated_at": {"type": "timestamp with time zone", "nullable": "NO"},
        },
        "pk": ["id"],
    },
    "fx_quotes": {
        "columns": {
            "from_currency": {"type": "character varying", "length": 3, "nullable": "NO"},
            "to_currency": {"type": "character varying", "length": 3, "nullable": "NO"},
            "rate_as_of": {"type": "date", "nullable": "NO"},
            "rate": {"type": "numeric", "nullable": "NO"},
            "source": {"type": "text", "nullable": "NO"},
            "fetched_at": {"type": "timestamp with time zone", "nullable": "NO"},
        },
        "pk": ["from_currency", "to_currency", "rate_as_of"],
    },
    "statement_lines": {
        "columns": {
            "id": {"type": "uuid", "nullable": "NO"},
            "household_id": {"type": "uuid", "nullable": "NO"},
            "request_id": {"type": "uuid", "nullable": "NO"},
            "account_id": {"type": "uuid", "nullable": "NO"},
            "row_no": {"type": "integer", "nullable": "NO"},
            "extracted_payload": {"type": "jsonb", "nullable": "NO"},
            "applied_transaction_id": {"type": "uuid", "nullable": "YES"},
            "final_action": {"type": "text", "nullable": "YES"},
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
            "source_request_id": {"type": "uuid", "nullable": "YES"},
            "entity_type": {"type": "text", "nullable": "NO"},
            "entity_id": {"type": "uuid", "nullable": "NO"},
            "action": {"type": "text", "nullable": "NO"},
            "before_data": {"type": "jsonb", "nullable": "YES"},
            "after_data": {"type": "jsonb", "nullable": "YES"},
            "reason": {"type": "text", "nullable": "YES"},
            "created_at": {"type": "timestamp with time zone", "nullable": "NO"},
        },
        "pk": ["id"],
    },
}

class TestSchemaParity(BaseDbTestCase):
    def test_database_schema_strict_parity(self):
        """
        Validates that the active test schema matches the 16 simplified tables,
        their column types, nullability, primary keys, and foreign keys.
        """
        with self.conn.cursor() as cur:
            # 1. Discover all base tables in the test schema (excluding schema_migrations)
            cur.execute("""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = %s
                  AND table_type = 'BASE TABLE'
                  AND table_name != 'schema_migrations';
            """, (self.test_schema,))
            actual_tables = {row[0] for row in cur.fetchall()}

            expected_tables = set(EXPECTED_TABLE_CONTRACTS.keys())
            self.assertEqual(
                actual_tables,
                expected_tables,
                f"Table mismatch. Unexpected: {actual_tables - expected_tables}, Missing: {expected_tables - actual_tables}"
            )

            # 2. Validate columns for each table
            for table_name, contract in EXPECTED_TABLE_CONTRACTS.items():
                cur.execute("""
                    SELECT column_name, data_type, is_nullable, character_maximum_length, numeric_precision, numeric_scale
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s;
                """, (self.test_schema, table_name))
                actual_columns = {
                    row[0]: {
                        "type": row[1],
                        "nullable": row[2],
                        "length": row[3],
                        "precision": row[4],
                        "scale": row[5]
                    } for row in cur.fetchall()
                }

                expected_columns = contract["columns"]
                self.assertEqual(
                    set(actual_columns.keys()),
                    set(expected_columns.keys()),
                    f"Columns mismatch in table '{table_name}'. "
                    f"Unexpected: {set(actual_columns.keys()) - set(expected_columns.keys())}, "
                    f"Missing: {set(expected_columns.keys()) - set(actual_columns.keys())}"
                )

                for col_name, exp in expected_columns.items():
                    act = actual_columns[col_name]
                    # Nullable check
                    self.assertEqual(
                        act["nullable"], exp["nullable"],
                        f"Nullability mismatch for {table_name}.{col_name}: expected {exp['nullable']}, got {act['nullable']}"
                    )
                    # Type check (allow 'character varying' / 'text' compatibility where expected)
                    if exp["type"] in ("text", "character varying"):
                        self.assertIn(
                            act["type"], ["text", "character varying"],
                            f"Type mismatch for {table_name}.{col_name}: expected {exp['type']}, got {act['type']}"
                        )
                    else:
                        self.assertEqual(
                            act["type"], exp["type"],
                            f"Type mismatch for {table_name}.{col_name}: expected {exp['type']}, got {act['type']}"
                        )

            # 3. Validate Primary Keys
            for table_name, contract in EXPECTED_TABLE_CONTRACTS.items():
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
                expected_pk = contract["pk"]
                self.assertEqual(
                    actual_pk, expected_pk,
                    f"Primary key mismatch in table '{table_name}': expected {expected_pk}, got {actual_pk}"
                )

            # 4. Foreign Key Delete Rules (All RESTRICT / NO ACTION in simplified schema)
            cur.execute("""
                SELECT tc.table_name, kcu.column_name, ccu.table_name AS foreign_table, rc.delete_rule
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
                JOIN information_schema.referential_constraints rc
                  ON tc.constraint_name = rc.constraint_name AND tc.table_schema = rc.constraint_schema
                JOIN information_schema.constraint_column_usage ccu
                  ON rc.unique_constraint_name = ccu.constraint_name AND rc.unique_constraint_schema = ccu.table_schema
                WHERE tc.table_schema = %s AND tc.constraint_type = 'FOREIGN KEY';
            """, (self.test_schema,))
            fk_rules = cur.fetchall()
            for tbl, col, foreign_tbl, delete_rule in fk_rules:
                self.assertIn(
                    delete_rule, ["RESTRICT", "NO ACTION"],
                    f"FK {tbl}.{col} -> {foreign_tbl} has delete rule {delete_rule}, must be RESTRICT or NO ACTION"
                )

            # 5. Audit Events Append-Only Trigger
            cur.execute("""
                SELECT trigger_name, event_manipulation, action_statement
                FROM information_schema.triggers
                WHERE trigger_schema = %s AND event_object_table = 'audit_events';
            """, (self.test_schema,))
            triggers = {row[0]: row[1] for row in cur.fetchall()}
            self.assertIn("trg_audit_events_immutable", triggers)

if __name__ == "__main__":
    unittest.main()
