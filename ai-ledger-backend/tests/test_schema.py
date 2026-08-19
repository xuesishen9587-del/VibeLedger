import os
os.environ["ENVIRONMENT"] = "test"
os.environ["DB_SCHEMA"] = "vibeledger_test_runner"

import unittest
import uuid
from decimal import Decimal
from datetime import date, datetime
import psycopg2
from psycopg2 import sql, errors
from app import config
from app.db import get_connection, transaction
from migrations import runner
from app.repositories import accounts, ingestion, audit

class TestSchemaAndRepository(unittest.TestCase):
    def setUp(self):
        if not config.is_safe_for_testing():
            self.skipTest("Skipping integration test. ENVIRONMENT must be 'test'.")
            
        self.test_schema = f"vibeledger_test_{uuid.uuid4().hex[:12]}"
        # Apply all migrations to this fresh schema
        runner.run_migrations(self.test_schema)
        self.conn = get_connection(self.test_schema)
        
    def tearDown(self):
        if hasattr(self, "conn") and self.conn:
            self.conn.close()
            
        if config.is_safe_for_testing() and hasattr(self, "test_schema"):
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

    # --- 1. Schema Parity Checks ---
    def test_schema_parity_against_physical_schema_spec(self):
        """
        Verify that column data types, precision, scale, and constraints
        comply exactly with docs/architecture/PHYSICAL_SCHEMA.md.
        """
        with self.conn.cursor() as cur:
            # Check transaction leg precision & scale: must be numeric(20,6)
            cur.execute("""
                SELECT data_type, numeric_precision, numeric_scale 
                FROM information_schema.columns 
                WHERE table_schema = %s AND table_name = 'transactions' AND column_name = 'original_amount';
            """, (self.test_schema,))
            row = cur.fetchone()
            self.assertEqual(row[0], 'numeric')
            self.assertEqual(row[1], 20)
            self.assertEqual(row[2], 6)
            
            # Check transaction FX rate precision & scale: must be numeric(24,12)
            cur.execute("""
                SELECT data_type, numeric_precision, numeric_scale 
                FROM information_schema.columns 
                WHERE table_schema = %s AND table_name = 'transactions' AND column_name = 'effective_fx_rate';
            """, (self.test_schema,))
            row = cur.fetchone()
            self.assertEqual(row[0], 'numeric')
            self.assertEqual(row[1], 24)
            self.assertEqual(row[2], 12)
            
            # Check currency column length: char(3)
            cur.execute("""
                SELECT data_type, character_maximum_length 
                FROM information_schema.columns 
                WHERE table_schema = %s AND table_name = 'accounts' AND column_name = 'currency';
            """, (self.test_schema,))
            row = cur.fetchone()
            self.assertEqual(row[0], 'character')
            self.assertEqual(row[1], 3)

            # Check audit_events ID type: bigint (BIGINT GENERATED ALWAYS AS IDENTITY)
            cur.execute("""
                SELECT data_type 
                FROM information_schema.columns 
                WHERE table_schema = %s AND table_name = 'audit_events' AND column_name = 'id';
            """, (self.test_schema,))
            row = cur.fetchone()
            self.assertEqual(row[0], 'bigint')

    # --- 2. Identity & Devices Constraints ---
    def test_identity_and_devices_constraints(self):
        h_id = uuid.uuid4()
        u_id = uuid.uuid4()
        
        # Create household & user
        accounts.create_household(self.conn, h_id, "Household 1", "CNY", date(2026, 1, 1))
        accounts.create_user(self.conn, u_id, "google|12345", "test@domain.com", "Test User")
        self.conn.commit()  # Save setup records so they aren't lost in rollback
        
        # Test Duplicate auth_subject rejected
        u_id2 = uuid.uuid4()
        with self.assertRaises(psycopg2.IntegrityError):
            accounts.create_user(self.conn, u_id2, "google|12345", "other@domain.com", "Other User")
        self.conn.rollback()

        # Add household membership
        accounts.add_household_member(self.conn, h_id, u_id, "owner")
        self.conn.commit()
        
        # Create device
        d_id = uuid.uuid4()
        token = b"mysecrettokenhash"
        accounts.create_device(self.conn, d_id, u_id, "iPhone 15", "ios_shortcuts", token)
        self.conn.commit()
        
        # Test Duplicate device token rejected
        d_id2 = uuid.uuid4()
        with self.assertRaises(psycopg2.IntegrityError):
            accounts.create_device(self.conn, d_id2, u_id, "iPad Pro", "ios_shortcuts", token)
        self.conn.rollback()
        
        # Verify device lookup
        dev = accounts.get_device_by_token_hash(self.conn, token)
        self.assertIsNotNone(dev)
        self.assertEqual(dev["id"], d_id)

    # --- 3. Accounts & Atomicity ---
    def test_accounts_atomicity_and_constraints(self):
        h_id = uuid.uuid4()
        u_id = uuid.uuid4()
        accounts.create_household(self.conn, h_id, "Household 2", "CNY", date(2026, 1, 1))
        accounts.create_user(self.conn, u_id, "google|45678", "test2@domain.com", "Test User 2")
        self.conn.commit()
        
        # Create Account: Atomically creates account and state
        acc_id = uuid.uuid4()
        accounts.create_account(self.conn, acc_id, h_id, "My Cash Account", None, "cash", "CNY", u_id)
        self.conn.commit()
        
        # Verify account created
        acc = accounts.get_account(self.conn, acc_id)
        self.assertIsNotNone(acc)
        self.assertEqual(acc["name"], "My Cash Account")
        
        # Verify state atomically created
        state = accounts.get_account_state(self.conn, acc_id)
        self.assertIsNotNone(state)
        self.assertEqual(state["ledger_balance"], Decimal("0.000000"))
        self.assertIsNone(state["initialized_at"])
        
        # Duplicate active account name in household rejected
        acc_id2 = uuid.uuid4()
        with self.assertRaises(psycopg2.IntegrityError):
            accounts.create_account(self.conn, acc_id2, h_id, "My Cash Account", None, "cash", "CNY", u_id)
        self.conn.rollback()

    # --- 4. Transaction Rollback & Concurrency Lock ---
    def test_transaction_rollback_and_locking(self):
        h_id = uuid.uuid4()
        accounts.create_household(self.conn, h_id, "Household 3", "CNY", date(2026, 1, 1))
        acc_id = uuid.uuid4()
        accounts.create_account(self.conn, acc_id, h_id, "Locked Account", None, "cash", "CNY")
        self.conn.commit()  # Save setup records
        
        # Test transaction rollback
        try:
            with transaction(self.conn):
                with self.conn.cursor() as cur:
                    # Modify state balance directly
                    cur.execute("UPDATE account_state SET ledger_balance = 50.000000 WHERE account_id = %s;", (acc_id,))
                    # Lock row
                    locked = accounts.lock_account_state(self.conn, acc_id)
                    self.assertEqual(locked["ledger_balance"], Decimal("50.000000"))
                    # Trigger error to force rollback
                    raise RuntimeError("Force Rollback")
        except RuntimeError:
            pass # Expected
            
        # Verify balance rolled back to 0
        state = accounts.get_account_state(self.conn, acc_id)
        self.assertEqual(state["ledger_balance"], Decimal("0.000000"))

    # --- 5. Categories Uniqueness ---
    def test_categories_constraints(self):
        h_id = uuid.uuid4()
        accounts.create_household(self.conn, h_id, "Household 4", "CNY", date(2026, 1, 1))
        self.conn.commit()
        
        cat_id1 = uuid.uuid4()
        accounts.create_category(self.conn, cat_id1, h_id, "Dining", "expense")
        self.conn.commit()
        
        # Duplicate category name under same type and household rejected
        cat_id2 = uuid.uuid4()
        with self.assertRaises(psycopg2.IntegrityError):
            accounts.create_category(self.conn, cat_id2, h_id, "dining", "expense") # lower-case check
        self.conn.rollback()

    # --- 6. Ingestion Requests Idempotency ---
    def test_ingestion_requests_constraints(self):
        h_id = uuid.uuid4()
        u_id = uuid.uuid4()
        d_id = uuid.uuid4()
        accounts.create_household(self.conn, h_id, "Household 5", "CNY", date(2026, 1, 1))
        accounts.create_user(self.conn, u_id, "google|999", "test9@domain.com", "Test User 9")
        accounts.create_device(self.conn, d_id, u_id, "Phone 5", "ios_shortcuts", b"token5")
        self.conn.commit()
        
        req_id1 = uuid.uuid4()
        ingestion.create_ingestion_request(
            self.conn, req_id1, d_id, "key-12345678", "expense", b"hash-111", "received", {"amount": 50}
        )
        self.conn.commit()
        
        # Verify retrieval
        req = ingestion.get_ingestion_request(self.conn, req_id1)
        self.assertIsNotNone(req)
        self.assertEqual(req["idempotency_key"], "key-12345678")
        
        # Duplicate device_id + idempotency_key rejected
        req_id2 = uuid.uuid4()
        with self.assertRaises(psycopg2.IntegrityError):
            ingestion.create_ingestion_request(
                self.conn, req_id2, d_id, "key-12345678", "expense", b"hash-222"
            )
        self.conn.rollback()

    # --- 7. Transactions Constraints ---
    def test_transactions_constraints(self):
        h_id = uuid.uuid4()
        accounts.create_household(self.conn, h_id, "Household 6", "CNY", date(2026, 1, 1))
        acc_id = uuid.uuid4()
        accounts.create_account(self.conn, acc_id, h_id, "Card Account", None, "cash", "CNY")
        self.conn.commit()
        
        tx_id = uuid.uuid4()
        # Try insert transaction with negative original amount -> must raise check constraint error
        with self.conn.cursor() as cur:
            with self.assertRaises(psycopg2.IntegrityError):
                cur.execute(
                    """
                    INSERT INTO transactions (
                        id, household_id, transaction_type, occurred_on, from_account_id, 
                        original_amount, original_currency, source, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
                    """,
                    (tx_id, h_id, "expense", date(2026, 1, 1), acc_id, -100.00, "CNY", "shortcut", "committed")
                )
            self.conn.rollback()

    # --- 8. Audit Event Trigger Immutability ---
    def test_audit_event_trigger_immutability(self):
        h_id = uuid.uuid4()
        accounts.create_household(self.conn, h_id, "Household 7", "CNY", date(2026, 1, 1))
        self.conn.commit()
        
        # Add audit event
        ae_id = uuid.uuid4() # We insert it and it generates a BIGINT identity PK
        audit.insert_audit_event(
            self.conn, h_id, "system", None, None, None, None, "account", ae_id, "create", None, {"name": "test"}
        )
        self.conn.commit()
        
        # Retrieve event ID
        events = audit.list_audit_events_for_entity(self.conn, "account", ae_id)
        self.assertEqual(len(events), 1)
        event_db_id = events[0]["id"]
        
        # Attempt update -> trigger must reject it raising a DatabaseError (specifically RaiseException)
        with self.conn.cursor() as cur:
            with self.assertRaises(psycopg2.DatabaseError) as ctx:
                cur.execute("UPDATE audit_events SET entity_type = 'hack' WHERE id = %s;", (event_db_id,))
            self.assertIn("audit_events is append-only", str(ctx.exception))
            self.conn.rollback()
            
        # Attempt delete -> trigger must reject it raising a DatabaseError
        with self.conn.cursor() as cur:
            with self.assertRaises(psycopg2.DatabaseError) as ctx:
                cur.execute("DELETE FROM audit_events WHERE id = %s;", (event_db_id,))
            self.assertIn("audit_events is append-only", str(ctx.exception))
            self.conn.rollback()
