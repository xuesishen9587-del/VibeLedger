import unittest
import uuid
from datetime import date
from decimal import Decimal
import psycopg2
from app.db import transaction
from app.repositories import accounts, audit
try:
    from tests.support.db_helper import BaseDbTestCase
except ModuleNotFoundError:
    from support.db_helper import BaseDbTestCase

class TestSchemaConstraints(BaseDbTestCase):
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

    def test_conservative_foreign_key_semantics(self):
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
