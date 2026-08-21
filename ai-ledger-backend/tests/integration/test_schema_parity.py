import unittest
try:
    from tests.support.db_helper import BaseDbTestCase
except ModuleNotFoundError:
    from support.db_helper import BaseDbTestCase

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

class TestSchemaParity(BaseDbTestCase):
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

    def test_catalog_structural_contracts(self):
        """
        Catalog-based assertions for key Phase 1 structural contracts:
        1. Key Column Defaults
        2. UNIQUE and Partial Indexes
        3. GIN Trigram Indexes
        4. Foreign Key Delete Actions (RESTRICT on ledger/audit, CASCADE on disposable children, SET NULL on actors)
        """
        with self.conn.cursor() as cur:
            # 1. Key Column Defaults
            cur.execute("""
                SELECT table_name, column_name, column_default
                FROM information_schema.columns
                WHERE table_schema = %s AND column_default IS NOT NULL;
            """, (self.test_schema,))
            defaults = {(row[0], row[1]): row[2] for row in cur.fetchall()}

            # Key status and version defaults
            self.assertIn("pending_first_bill", defaults.get(("installment_plans", "status"), ""))
            self.assertIn("1", defaults.get(("reconciliation_batches", "engine_version"), ""))
            self.assertIn("0", defaults.get(("account_state", "ledger_balance"), ""))
            self.assertIn("0", defaults.get(("accounts", "row_version"), ""))
            self.assertIn("0", defaults.get(("account_state", "row_version"), ""))
            self.assertIn("0", defaults.get(("transactions", "row_version"), ""))
            self.assertIn("0", defaults.get(("reconciliation_batches", "row_version"), ""))
            self.assertIn("active", defaults.get(("accounts", "status"), ""))
            self.assertIn("active", defaults.get(("categories", "status"), ""))
            self.assertIn("committed", defaults.get(("transactions", "status"), ""))
            self.assertIn("unverified", defaults.get(("transactions", "verification_status"), ""))
            self.assertIn("scheduled", defaults.get(("installment_periods", "status"), ""))
            self.assertIn("unmatched", defaults.get(("statement_lines", "match_status"), ""))
            self.assertIn("proposed", defaults.get(("reconciliation_candidates", "status"), ""))

            # 2. Indexes: Unique, Partial, and Trigram GIN
            cur.execute("""
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = %s;
            """, (self.test_schema,))
            index_defs = {row[0]: row[1] for row in cur.fetchall()}

            # Unique partial indexes
            self.assertIn("uq_accounts_active_name", index_defs)
            self.assertIn("UNIQUE INDEX", index_defs["uq_accounts_active_name"])
            self.assertIn("status = 'active'", index_defs["uq_accounts_active_name"])

            self.assertIn("uq_categories_active", index_defs)
            self.assertIn("UNIQUE INDEX", index_defs["uq_categories_active"])
            self.assertIn("status = 'active'", index_defs["uq_categories_active"])

            self.assertIn("uq_account_alias", index_defs)
            self.assertIn("UNIQUE INDEX", index_defs["uq_account_alias"])
            self.assertIn("deleted_at IS NULL", index_defs["uq_account_alias"])

            self.assertIn("uq_snapshot_per_batch", index_defs)
            self.assertIn("UNIQUE INDEX", index_defs["uq_snapshot_per_batch"])
            self.assertIn("reconciliation_batch_id IS NOT NULL", index_defs["uq_snapshot_per_batch"])

            self.assertIn("uq_credit_snapshot_per_batch", index_defs)
            self.assertIn("UNIQUE INDEX", index_defs["uq_credit_snapshot_per_batch"])
            self.assertIn("reconciliation_batch_id IS NOT NULL", index_defs["uq_credit_snapshot_per_batch"])

            self.assertIn("uq_transaction_link_source_relation", index_defs)
            self.assertIn("UNIQUE INDEX", index_defs["uq_transaction_link_source_relation"])

            # GIN Trigram indexes
            self.assertIn("ix_transactions_merchant_trgm", index_defs)
            self.assertIn("using gin", index_defs["ix_transactions_merchant_trgm"].lower())
            self.assertIn("gin_trgm_ops", index_defs["ix_transactions_merchant_trgm"])

            self.assertIn("ix_statement_description_trgm", index_defs)
            self.assertIn("using gin", index_defs["ix_statement_description_trgm"].lower())
            self.assertIn("gin_trgm_ops", index_defs["ix_statement_description_trgm"])

            self.assertIn("ix_account_alias_trgm", index_defs)
            self.assertIn("using gin", index_defs["ix_account_alias_trgm"].lower())
            self.assertIn("gin_trgm_ops", index_defs["ix_account_alias_trgm"])

            # 3. Foreign Key Delete Rules (RESTRICT / CASCADE / SET NULL)
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
            fk_rules = {(row[0], row[1], row[2]): row[3] for row in cur.fetchall()}

            # RESTRICT (or NO ACTION): Durable ledger facts and audit log
            self.assertIn(fk_rules.get(("audit_events", "household_id", "households")), ["RESTRICT", "NO ACTION"])
            self.assertIn(fk_rules.get(("transactions", "household_id", "households")), ["RESTRICT", "NO ACTION"])
            self.assertIn(fk_rules.get(("transactions", "from_account_id", "accounts")), ["RESTRICT", "NO ACTION"])
            self.assertIn(fk_rules.get(("transactions", "to_account_id", "accounts")), ["RESTRICT", "NO ACTION"])
            self.assertIn(fk_rules.get(("transactions", "category_id", "categories")), ["RESTRICT", "NO ACTION"])
            self.assertIn(fk_rules.get(("accounts", "household_id", "households")), ["RESTRICT", "NO ACTION"])
            self.assertIn(fk_rules.get(("categories", "household_id", "households")), ["RESTRICT", "NO ACTION"])
            self.assertIn(fk_rules.get(("installment_plans", "household_id", "households")), ["RESTRICT", "NO ACTION"])
            self.assertIn(fk_rules.get(("installment_plans", "credit_account_id", "accounts")), ["RESTRICT", "NO ACTION"])
            self.assertIn(fk_rules.get(("reconciliation_batches", "household_id", "households")), ["RESTRICT", "NO ACTION"])
            self.assertIn(fk_rules.get(("reconciliation_batches", "account_id", "accounts")), ["RESTRICT", "NO ACTION"])

            # CASCADE: Disposable or tightly bound subordinate children
            self.assertEqual(fk_rules.get(("household_members", "household_id", "households")), "CASCADE")
            self.assertEqual(fk_rules.get(("household_members", "user_id", "users")), "CASCADE")
            self.assertEqual(fk_rules.get(("devices", "user_id", "users")), "CASCADE")
            self.assertEqual(fk_rules.get(("account_state", "account_id", "accounts")), "CASCADE")
            self.assertEqual(fk_rules.get(("account_aliases", "account_id", "accounts")), "CASCADE")
            self.assertEqual(fk_rules.get(("installment_periods", "plan_id", "installment_plans")), "CASCADE")
            self.assertEqual(fk_rules.get(("statement_lines", "batch_id", "reconciliation_batches")), "CASCADE")
            self.assertEqual(fk_rules.get(("reconciliation_candidates", "batch_id", "reconciliation_batches")), "CASCADE")

            # SET NULL: Nullable actor / request / batch links preserve audit history and ledger evidence
            self.assertEqual(fk_rules.get(("audit_events", "actor_user_id", "users")), "SET NULL")
            self.assertEqual(fk_rules.get(("audit_events", "actor_device_id", "devices")), "SET NULL")
            self.assertEqual(fk_rules.get(("audit_events", "request_id", "ingestion_requests")), "SET NULL")
            self.assertEqual(fk_rules.get(("audit_events", "reconciliation_batch_id", "reconciliation_batches")), "SET NULL")
            self.assertEqual(fk_rules.get(("transactions", "source_request_id", "ingestion_requests")), "SET NULL")
            self.assertEqual(fk_rules.get(("transactions", "statement_batch_id", "reconciliation_batches")), "SET NULL")
            self.assertEqual(fk_rules.get(("transactions", "created_by_user_id", "users")), "SET NULL")
            self.assertEqual(fk_rules.get(("transactions", "created_by_device_id", "devices")), "SET NULL")
            self.assertEqual(fk_rules.get(("transactions", "deleted_by_user_id", "users")), "SET NULL")
