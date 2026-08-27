import os
import unittest
from uuid import uuid4
from decimal import Decimal
from datetime import date, datetime, timezone
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.db import get_connection
from app.main import create_app
from app.api.deps import get_db_connection
from app.auth.browser_verifier import set_browser_verifier, StaticBrowserAuthVerifier
from app.repositories import users as users_repo
from app.repositories import household_members as members_repo
from app.repositories import accounts as accounts_repo
from app.repositories import reconciliation as reconciliation_repo
import app.repositories.work_queue as work_queue_repo
from tests.support.db_helper import BaseDbTestCase


class TestWorkQueueAndReadinessDb(BaseDbTestCase):
    @classmethod
    def cls_setup(cls):
        cls.app = create_app()
        cls.client = TestClient(cls.app)

        def _get_db():
            conn = get_connection(cls.test_schema)
            try:
                yield conn
            finally:
                if not conn.closed:
                    conn.close()

        os.environ["DB_SCHEMA"] = cls.test_schema
        try:
            from app.config import get_settings
            s = get_settings()
            if s:
                s.db_schema = cls.test_schema
        except Exception:
            pass

        cls.app.dependency_overrides[get_db_connection] = _get_db
        cls.static_verifier = StaticBrowserAuthVerifier()
        set_browser_verifier(cls.static_verifier)

    @classmethod
    def tearDownClass(cls):
        set_browser_verifier(None)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        os.environ["DB_SCHEMA"] = self.test_schema
        try:
            from app.config import get_settings
            s = get_settings()
            if s:
                s.DB_SCHEMA = self.test_schema
                s.db_schema = self.test_schema
        except Exception:
            pass
        self.household_id = uuid4()
        members_repo.create_household(
            self.conn,
            household_id=self.household_id,
            name="Work Queue Household",
            reporting_currency="CNY",
            ledger_start_date=date(2026, 1, 1),
            status="active"
        )
        self.user_id = uuid4()
        self.auth_subject = "auth0|wq_user"
        users_repo.create_user(
            self.conn,
            user_id=self.user_id,
            auth_subject=self.auth_subject,
            display_name="WQ User",
            email="wq@example.com",
            default_currency="CNY",
            status="active"
        )
        members_repo.add_household_member(
            self.conn,
            household_id=self.household_id,
            user_id=self.user_id,
            role="owner"
        )
        self.account_id = uuid4()
        accounts_repo.create_account(
            self.conn,
            account_id=self.account_id,
            household_id=self.household_id,
            name="ICBC Checking",
            institution="ICBC",
            account_type="cash",
            currency="CNY",
            owner_user_id=self.user_id
        )
        self.browser_token = "wq.jwt.token"
        self.static_verifier.register_token(
            self.browser_token,
            {"sub": self.auth_subject, "exp": 9999999999}
        )
        self.conn.commit()

    def test_work_queue_filtering_and_separation(self):
        headers = {"Authorization": f"Bearer {self.browser_token}"}

        # 1. Initially empty
        resp = self.client.get("/api/v1/work-queue", headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["items"], [])

        # 2. Insert a reconciliation batch in 'needs_review'
        batch_id = uuid4()
        reconciliation_repo.create_reconciliation_batch(
            self.conn,
            batch_id=batch_id,
            household_id=self.household_id,
            account_id=self.account_id,
            batch_type="statement",
            status="needs_review",
            currency="CNY",
            statement_balance=Decimal("5000.00"),
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31)
        )
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE reconciliation_batches
                SET pending_count = 3
                WHERE id = %s;
                """,
                (batch_id,)
            )
        self.conn.commit()

        # 3. Query all work queue
        resp_all = self.client.get("/api/v1/work-queue", headers=headers)
        self.assertEqual(resp_all.status_code, 200)
        items = resp_all.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["work_type"], "reconciliation")
        self.assertEqual(items[0]["id"], str(batch_id))

        # 4. Query with filter type=reconciliation
        resp_rec = self.client.get("/api/v1/work-queue?type=reconciliation", headers=headers)
        self.assertEqual(resp_rec.status_code, 200)
        self.assertEqual(len(resp_rec.json()["items"]), 1)

        # 5. Query with filter type=ingestion -> empty
        resp_ing = self.client.get("/api/v1/work-queue?type=ingestion", headers=headers)
        self.assertEqual(resp_ing.status_code, 200)
        self.assertEqual(len(resp_ing.json()["items"]), 0)

        # 6. Query with invalid type filter -> 422
        resp_invalid = self.client.get("/api/v1/work-queue?type=invalid_type", headers=headers)
        self.assertEqual(resp_invalid.status_code, 422)

    def test_readiness_endpoint(self):
        # 1. Healthy database and default env
        resp = self.client.get("/ready")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["database"], "ok")
        self.assertIn(data["status"], ("ok", "degraded"))
        self.assertIn(data["gemini"], ("ok", "unavailable"))

        # 2. Test degraded status when GEMINI_API_KEY is not set
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False):
            resp_deg = self.client.get("/ready")
            self.assertEqual(resp_deg.status_code, 200)
            self.assertEqual(resp_deg.json()["status"], "degraded")
            self.assertEqual(resp_deg.json()["gemini"], "unavailable")
            self.assertEqual(resp_deg.json()["database"], "ok")

        # 3. Test ok status when GEMINI_API_KEY is configured
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test_mock_gemini_key"}, clear=False):
            resp_ok = self.client.get("/ready")
            self.assertEqual(resp_ok.status_code, 200)
            self.assertEqual(resp_ok.json()["status"], "ok")
            self.assertEqual(resp_ok.json()["gemini"], "ok")
            self.assertEqual(resp_ok.json()["database"], "ok")

        # 4. Connection acquisition failure -> 503 unavailable
        with patch("app.db.get_connection", side_effect=Exception("Database pool exhausted")):
            resp_conn_fail = self.client.get("/ready")
            self.assertEqual(resp_conn_fail.status_code, 503)
            self.assertEqual(resp_conn_fail.json()["status"], "unavailable")
            self.assertEqual(resp_conn_fail.json()["database"], "unavailable")

        # 5. Query execution failure -> 503 unavailable
        mock_bad_conn = MagicMock()
        mock_bad_cur = MagicMock()
        mock_bad_cur.execute.side_effect = Exception("Fatal database query error")
        mock_bad_conn.cursor.return_value.__enter__.return_value = mock_bad_cur
        with patch("app.db.get_connection", return_value=mock_bad_conn):
            resp_query_fail = self.client.get("/ready")
            self.assertEqual(resp_query_fail.status_code, 503)
            self.assertEqual(resp_query_fail.json()["status"], "unavailable")
            self.assertEqual(resp_query_fail.json()["database"], "unavailable")

        # 6. Missing schema_migrations table -> 503 schema_not_ready
        mock_no_table_conn = MagicMock()
        mock_no_table_cur = MagicMock()
        mock_no_table_cur.execute.return_value = None
        mock_no_table_cur.fetchone.return_value = [False]
        mock_no_table_conn.cursor.return_value.__enter__.return_value = mock_no_table_cur
        with patch("app.db.get_connection", return_value=mock_no_table_conn):
            resp_no_table = self.client.get("/ready")
            self.assertEqual(resp_no_table.status_code, 503)
            self.assertEqual(resp_no_table.json()["status"], "unavailable")
            self.assertEqual(resp_no_table.json()["database"], "schema_not_ready")

        # 7. Missing/stale migration version -> 503 schema_not_ready
        mock_stale_conn = MagicMock()
        mock_stale_cur = MagicMock()
        mock_stale_cur.execute.return_value = None
        mock_stale_cur.fetchone.return_value = [True]
        mock_stale_cur.fetchall.return_value = [("0001_extensions.sql", "valid_hash")]  # Only 1 of 9 migrations applied
        mock_stale_conn.cursor.return_value.__enter__.return_value = mock_stale_cur
        with patch("app.db.get_connection", return_value=mock_stale_conn):
            resp_stale = self.client.get("/ready")
            self.assertEqual(resp_stale.status_code, 503)
            self.assertEqual(resp_stale.json()["status"], "unavailable")
            self.assertEqual(resp_stale.json()["database"], "schema_not_ready")

        # 8. Same filename, wrong checksum -> 503 schema_not_ready
        from migrations.runner import get_migration_files
        all_files = get_migration_files()
        tampered_rows = [(f, "tampered_bad_checksum_hash") for f in all_files]
        mock_drift_conn = MagicMock()
        mock_drift_cur = MagicMock()
        mock_drift_cur.execute.return_value = None
        mock_drift_cur.fetchone.return_value = [True]
        mock_drift_cur.fetchall.return_value = tampered_rows
        mock_drift_conn.cursor.return_value.__enter__.return_value = mock_drift_cur
        with patch("app.db.get_connection", return_value=mock_drift_conn):
            resp_drift = self.client.get("/ready")
            self.assertEqual(resp_drift.status_code, 503)
            self.assertEqual(resp_drift.json()["status"], "unavailable")
            self.assertEqual(resp_drift.json()["database"], "schema_not_ready")


if __name__ == "__main__":
    unittest.main()
