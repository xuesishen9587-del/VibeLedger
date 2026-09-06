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
        accounts.create_household(self.conn, h_id, "HH_NotNull", reporting_currency="CNY")
        accounts.create_user(self.conn, u_id, "auth_not_null", "User_NotNull")
        accounts.add_household_member(self.conn, h_id, u_id, "owner")
        accounts.create_device(self.conn, d_id, u_id, "Dev_NotNull", "ios_shortcuts", b"hash_notnull", household_id=h_id)
        self.conn.commit()

        test_null_inserts = [
            ("households", "INSERT INTO households (id, name, reporting_currency, status) VALUES (%s, %s, %s, NULL);", (uuid.uuid4(), "H", "CNY")),
            ("users", "INSERT INTO users (id, auth_subject, display_name, status) VALUES (%s, %s, %s, NULL);", (uuid.uuid4(), "sub1", "U")),
            ("household_members", "INSERT INTO household_members (household_id, user_id, role) VALUES (%s, %s, NULL);", (h_id, u_id)),
            ("devices", "INSERT INTO devices (id, household_id, user_id, device_name, platform, token_hash, status) VALUES (%s, %s, %s, %s, %s, %s, NULL);", (uuid.uuid4(), h_id, u_id, "D", "ios_shortcuts", b"t1")),
            ("devices", "INSERT INTO devices (id, household_id, user_id, device_name, platform, token_hash, status) VALUES (%s, %s, %s, %s, NULL, %s, %s);", (uuid.uuid4(), h_id, u_id, "D", b"t2", "active")),
            ("accounts", "INSERT INTO accounts (id, household_id, name, balance_scope, account_type, currency, status, opened_on) VALUES (%s, %s, %s, %s, NULL, %s, %s, %s);", (uuid.uuid4(), h_id, "A1", "Scope", "CNY", "active", date(2026, 1, 1))),
            ("accounts", "INSERT INTO accounts (id, household_id, name, balance_scope, account_type, currency, status, opened_on) VALUES (%s, %s, %s, %s, %s, %s, NULL, %s);", (uuid.uuid4(), h_id, "A2", "Scope", "cash", "CNY", date(2026, 1, 1))),
            ("categories", "INSERT INTO categories (id, household_id, name, category_type, status) VALUES (%s, %s, %s, NULL, %s);", (uuid.uuid4(), h_id, "C1", "active")),
            ("categories", "INSERT INTO categories (id, household_id, name, category_type, status) VALUES (%s, %s, %s, %s, NULL);", (uuid.uuid4(), h_id, "C2", "expense")),
            ("ingestion_requests", "INSERT INTO ingestion_requests (id, household_id, user_id, actor_scope, idempotency_key, request_kind, operation, status) VALUES (%s, %s, %s, %s, %s, NULL, %s, %s);", (uuid.uuid4(), h_id, u_id, "user:1", "key1234567", "cancel", "processing")),
            ("ingestion_requests", "INSERT INTO ingestion_requests (id, household_id, user_id, actor_scope, idempotency_key, request_kind, operation, status) VALUES (%s, %s, %s, %s, %s, %s, %s, NULL);", (uuid.uuid4(), h_id, u_id, "user:1", "key1234568", "command", "cancel")),
            ("audit_events", "INSERT INTO audit_events (household_id, actor_type, entity_type, entity_id, action) VALUES (%s, NULL, %s, %s, %s);", (h_id, "account", uuid.uuid4(), "create")),
            ("audit_events", "INSERT INTO audit_events (household_id, actor_type, entity_type, entity_id, action) VALUES (%s, %s, %s, %s, NULL);", (h_id, "system", "account", uuid.uuid4())),
        ]

        with self.conn.cursor() as cur:
            for tbl, sql_stmt, params in test_null_inserts:
                with self.assertRaises(psycopg2.IntegrityError, msg=f"Table {tbl} allowed NULL for required field"):
                    cur.execute(sql_stmt, params)
                self.conn.rollback()

    def test_conservative_foreign_key_semantics(self):
        h_id = uuid.uuid4()
        u_id = uuid.uuid4()
        d_id = uuid.uuid4()
        acc_id = uuid.uuid4()
        cat_id = uuid.uuid4()
        req_id = uuid.uuid4()

        accounts.create_household(self.conn, h_id, "HH_FK", reporting_currency="CNY")
        accounts.create_user(self.conn, u_id, "auth_fk", "User_FK")
        accounts.add_household_member(self.conn, h_id, u_id, "owner")
        accounts.create_device(self.conn, d_id, u_id, "Dev_FK", "ios_shortcuts", b"hash_fk", household_id=h_id)
        accounts.create_account(self.conn, acc_id, h_id, "Card_FK", "credit", "CNY", balance_scope="Credit Cards")
        accounts.create_category(self.conn, cat_id, h_id, "Cat_FK", "expense")

        with self.conn.cursor() as cur:
            cur.execute("""
                INSERT INTO ingestion_requests (
                    id, household_id, user_id, device_id, actor_scope, idempotency_key, request_kind, operation, request_hash, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
            """, (req_id, h_id, u_id, d_id, "device:1", "req_fk_123456", "command", "test", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", "processing"))
        self.conn.commit()

        # 1. Attempting to delete account with transactions must fail (RESTRICT)
        tx_id = uuid.uuid4()
        with self.conn.cursor() as cur:
            cur.execute("""
                INSERT INTO transactions (
                    id, household_id, transaction_type, occurred_on, account_id, category_id,
                    original_amount, original_currency, date_source, source, status,
                    created_by_user_id, source_request_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
            """, (tx_id, h_id, "expense", date(2026, 1, 1), acc_id, cat_id, 50.0, "CNY", "receipt", "shortcut", "committed", u_id, req_id))
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

        # 3. Attempting to delete user referenced by household_members must fail (RESTRICT)
        with self.conn.cursor() as cur:
            with self.assertRaises(psycopg2.IntegrityError):
                cur.execute("DELETE FROM users WHERE id = %s;", (u_id,))
        self.conn.rollback()

    def test_audit_event_trigger_immutability(self):
        h_id = uuid.uuid4()
        accounts.create_household(self.conn, h_id, "HH_Audit", reporting_currency="CNY")
        self.conn.commit()

        ae_id = uuid.uuid4()
        audit.insert_audit_event(
            self.conn, h_id, "system", "account", ae_id, "create", after_data={"name": "test"}
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

    def test_account_active_name_case_insensitive_uniqueness(self):
        h_id = uuid.uuid4()
        accounts.create_household(self.conn, h_id, "HH_AccName", reporting_currency="CNY")
        self.conn.commit()

        acc1_id = uuid.uuid4()
        acc2_id = uuid.uuid4()
        accounts.create_account(self.conn, acc1_id, h_id, "Checking Account", "cash", "CNY", balance_scope="Scope")
        self.conn.commit()

        with self.conn.cursor() as cur:
            with self.assertRaises(psycopg2.IntegrityError):
                cur.execute("""
                    INSERT INTO accounts (id, household_id, name, balance_scope, account_type, currency, opened_on, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'active');
                """, (acc2_id, h_id, "checking account", "Scope", "cash", "CNY", date(2026, 1, 1)))
        self.conn.rollback()

    def test_category_active_type_name_uniqueness(self):
        h_id = uuid.uuid4()
        accounts.create_household(self.conn, h_id, "HH_CatName", reporting_currency="CNY")
        self.conn.commit()

        cat1_id = uuid.uuid4()
        cat2_id = uuid.uuid4()
        accounts.create_category(self.conn, cat1_id, h_id, "Dining", "expense")
        self.conn.commit()

        with self.conn.cursor() as cur:
            with self.assertRaises(psycopg2.IntegrityError):
                cur.execute("""
                    INSERT INTO categories (id, household_id, name, category_type, status)
                    VALUES (%s, %s, %s, 'expense', 'active');
                """, (cat2_id, h_id, "dining"))
        self.conn.rollback()

    def test_account_alias_active_uniqueness(self):
        h_id = uuid.uuid4()
        accounts.create_household(self.conn, h_id, "HH_Alias", reporting_currency="CNY")
        acc_id = uuid.uuid4()
        accounts.create_account(self.conn, acc_id, h_id, "Main Bank", "cash", "CNY", balance_scope="Scope")
        self.conn.commit()

        al1_id = uuid.uuid4()
        al2_id = uuid.uuid4()
        accounts.create_account_alias(self.conn, al1_id, acc_id, "Card1", "card1", "active", household_id=h_id)
        self.conn.commit()

        with self.conn.cursor() as cur:
            with self.assertRaises(psycopg2.IntegrityError):
                cur.execute("""
                    INSERT INTO account_aliases (id, household_id, account_id, alias_text, normalized_alias, status)
                    VALUES (%s, %s, %s, %s, %s, 'active');
                """, (al2_id, h_id, acc_id, "CARD1", "card1"))
        self.conn.rollback()

if __name__ == "__main__":
    unittest.main()
