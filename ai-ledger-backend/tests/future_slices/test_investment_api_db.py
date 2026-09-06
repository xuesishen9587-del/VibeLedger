import os
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test_user:test_pass@127.0.0.1:5432/vibeledger_test")
os.environ.setdefault("DB_SCHEMA", "vibeledger_test_runner")

import hashlib
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional, Dict
from uuid import UUID, uuid4
from fastapi.testclient import TestClient

from app.main import create_app
from app.db import get_connection, transaction
from app.api.deps import get_db_connection
from tests.support.db_helper import BaseDbTestCase
import app.repositories.accounts as accounts_repo
import app.repositories.devices as devices_repo
import app.repositories.transactions as tx_repo


class TestInvestmentApiDb(BaseDbTestCase):
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
        cls.app.dependency_overrides[get_db_connection] = _get_db

    def setUp(self):
        super().setUp()
        self.seed_test_data()

    def seed_test_data(self):
        self.household_id = uuid4()
        self.user_id = uuid4()
        self.device_id = uuid4()
        self.raw_token = f"vbl_test_{uuid4().hex}"
        self.token_hash = hashlib.sha256(self.raw_token.encode("utf-8")).digest()
        self.headers = {"Authorization": f"Bearer {self.raw_token}"}

        # Household B for cross-household isolation
        self.household_b_id = uuid4()
        self.user_b_id = uuid4()
        self.device_b_id = uuid4()
        self.raw_token_b = f"vbl_test_{uuid4().hex}"
        self.token_b_hash = hashlib.sha256(self.raw_token_b.encode("utf-8")).digest()
        self.headers_b = {"Authorization": f"Bearer {self.raw_token_b}"}

        self.acc_invest_cny_id = uuid4()
        self.acc_invest_usd_id = uuid4()
        self.acc_cash_id = uuid4()
        self.acc_inactive_id = uuid4()
        self.acc_b_id = uuid4()

        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                # Household A
                unique_a = uuid4().hex[:8]
                accounts_repo.create_household(conn, self.household_id, f"Household A {unique_a}", date(2026, 1, 1), "CNY")
                accounts_repo.create_user(conn, self.user_id, f"user_a_{unique_a}", "User A", f"user_a_{unique_a}@test.local", "CNY")
                accounts_repo.add_user_to_household(conn, self.household_id, self.user_id, role="owner")
                devices_repo.create_device(conn, self.device_id, self.user_id, "iPhone A", self.token_hash)

                # Household B
                unique_b = uuid4().hex[:8]
                accounts_repo.create_household(conn, self.household_b_id, f"Household B {unique_b}", date(2026, 1, 1), "CNY")
                accounts_repo.create_user(conn, self.user_b_id, f"user_b_{unique_b}", "User B", f"user_b_{unique_b}@test.local", "CNY")
                accounts_repo.add_user_to_household(conn, self.household_b_id, self.user_b_id, role="owner")
                devices_repo.create_device(conn, self.device_b_id, self.user_b_id, "iPhone B", self.token_b_hash)

                # Accounts
                accounts_repo.create_account(
                    conn=conn,
                    account_id=self.acc_invest_cny_id,
                    household_id=self.household_id,
                    name="Stock Investment CNY",
                    account_type="investment",
                    currency="CNY",
                    owner_user_id=self.user_id
                )
                accounts_repo.create_account(
                    conn=conn,
                    account_id=self.acc_invest_usd_id,
                    household_id=self.household_id,
                    name="IBKR USD",
                    account_type="investment",
                    currency="USD",
                    owner_user_id=self.user_id
                )
                accounts_repo.create_account(
                    conn=conn,
                    account_id=self.acc_cash_id,
                    household_id=self.household_id,
                    name="Checking Account",
                    account_type="cash",
                    currency="CNY",
                    owner_user_id=self.user_id
                )
                accounts_repo.create_account(
                    conn=conn,
                    account_id=self.acc_inactive_id,
                    household_id=self.household_id,
                    name="Closed Account",
                    account_type="investment",
                    currency="CNY",
                    owner_user_id=self.user_id,
                    status="inactive"
                )
                accounts_repo.create_account(
                    conn=conn,
                    account_id=self.acc_b_id,
                    household_id=self.household_b_id,
                    name="Account B",
                    account_type="investment",
                    currency="CNY",
                    owner_user_id=self.user_b_id
                )
        finally:
            conn.close()

    def test_create_first_manual_snapshot_api_success(self):
        payload = {
            "idempotency_key": "api-inv-snap-001",
            "as_of": "2026-08-01T10:00:00+08:00",
            "total_asset_value": "100000.00",
            "currency": "CNY",
            "source": "dashboard_manual"
        }
        res = self.client.post(
            f"/api/v1/investment-accounts/{self.acc_invest_cny_id}/snapshots",
            headers=self.headers,
            json=payload
        )
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(data["status"], "committed")
        self.assertIsNotNone(data["snapshot_id"])
        self.assertIsNone(data["investment_pnl"])

    def test_create_subsequent_snapshot_api_with_pnl(self):
        # 1. First snapshot
        self.client.post(
            f"/api/v1/investment-accounts/{self.acc_invest_cny_id}/snapshots",
            headers=self.headers,
            json={
                "idempotency_key": "api-inv-snap-002a",
                "as_of": "2026-08-01T10:00:00+08:00",
                "total_asset_value": "100000.00",
                "currency": "CNY",
                "source": "dashboard_manual"
            }
        )

        # 2. Add committed transfer
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=uuid4(),
                    household_id=self.household_id,
                    transaction_type="transfer",
                    occurred_on=date(2026, 8, 10),
                    original_amount=Decimal("50000.00"),
                    original_currency="CNY",
                    from_account_id=self.acc_cash_id,
                    to_account_id=self.acc_invest_cny_id,
                    from_amount=Decimal("50000.00"),
                    from_currency="CNY",
                    to_amount=Decimal("50000.00"),
                    to_currency="CNY",
                    status="committed"
                )
        finally:
            conn.close()

        # 3. Second snapshot
        res = self.client.post(
            f"/api/v1/investment-accounts/{self.acc_invest_cny_id}/snapshots",
            headers=self.headers,
            json={
                "idempotency_key": "api-inv-snap-002b",
                "as_of": "2026-08-20T10:00:00+08:00",
                "total_asset_value": "160000.00",
                "currency": "CNY",
                "source": "dashboard_manual"
            }
        )
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(data["status"], "committed")
        self.assertIsNotNone(data["investment_pnl"])
        self.assertEqual(data["investment_pnl"]["pnl_amount"], "10000.00")
        self.assertEqual(data["investment_pnl"]["status"], "confirmed")

    def test_manual_snapshot_idempotency_same_payload_returns_cached_response(self):
        payload = {
            "idempotency_key": "api-inv-snap-idem-001",
            "as_of": "2026-08-01T10:00:00+08:00",
            "total_asset_value": "100000.00",
            "currency": "CNY",
            "source": "dashboard_manual"
        }
        res1 = self.client.post(
            f"/api/v1/investment-accounts/{self.acc_invest_cny_id}/snapshots",
            headers=self.headers,
            json=payload
        )
        self.assertEqual(res1.status_code, 201)

        res2 = self.client.post(
            f"/api/v1/investment-accounts/{self.acc_invest_cny_id}/snapshots",
            headers=self.headers,
            json=payload
        )
        self.assertEqual(res2.status_code, 201)
        self.assertEqual(res1.json()["snapshot_id"], res2.json()["snapshot_id"])

    def test_manual_snapshot_idempotency_different_payload_returns_409(self):
        payload1 = {
            "idempotency_key": "api-inv-snap-idem-conflict",
            "as_of": "2026-08-01T10:00:00+08:00",
            "total_asset_value": "100000.00",
            "currency": "CNY",
            "source": "dashboard_manual"
        }
        res1 = self.client.post(
            f"/api/v1/investment-accounts/{self.acc_invest_cny_id}/snapshots",
            headers=self.headers,
            json=payload1
        )
        self.assertEqual(res1.status_code, 201)

        payload2 = dict(payload1)
        payload2["total_asset_value"] = "150000.00"

        res2 = self.client.post(
            f"/api/v1/investment-accounts/{self.acc_invest_cny_id}/snapshots",
            headers=self.headers,
            json=payload2
        )
        self.assertEqual(res2.status_code, 409)
        err = res2.json()
        self.assertEqual(err["error"]["code"], "IDEMPOTENCY_KEY_REUSE")

    def test_manual_snapshot_non_investment_account_returns_422_type_mismatch(self):
        payload = {
            "idempotency_key": "api-inv-snap-mismatch",
            "as_of": "2026-08-01T10:00:00+08:00",
            "total_asset_value": "100000.00",
            "currency": "CNY"
        }
        res = self.client.post(
            f"/api/v1/investment-accounts/{self.acc_cash_id}/snapshots",
            headers=self.headers,
            json=payload
        )
        self.assertEqual(res.status_code, 422)
        err = res.json()
        self.assertEqual(err["error"]["code"], "ACCOUNT_TYPE_MISMATCH")

    def test_manual_snapshot_inactive_account_returns_422(self):
        payload = {
            "idempotency_key": "api-inv-snap-inactive",
            "as_of": "2026-08-01T10:00:00+08:00",
            "total_asset_value": "100000.00",
            "currency": "CNY"
        }
        res = self.client.post(
            f"/api/v1/investment-accounts/{self.acc_inactive_id}/snapshots",
            headers=self.headers,
            json=payload
        )
        self.assertEqual(res.status_code, 422)
        err = res.json()
        self.assertEqual(err["error"]["code"], "ACCOUNT_INACTIVE")

    def test_manual_snapshot_cross_household_returns_404(self):
        payload = {
            "idempotency_key": "api-inv-snap-cross",
            "as_of": "2026-08-01T10:00:00+08:00",
            "total_asset_value": "100000.00",
            "currency": "CNY"
        }
        res = self.client.post(
            f"/api/v1/investment-accounts/{self.acc_b_id}/snapshots",
            headers=self.headers,
            json=payload
        )
        self.assertEqual(res.status_code, 404)
        err = res.json()
        self.assertEqual(err["error"]["code"], "ACCOUNT_NOT_FOUND")

    def test_generic_snapshot_endpoint_still_rejects_investment_account(self):
        """
        Section 50: Generic ordinary balance snapshot endpoint must CONTINUE rejecting investment accounts.
        """
        payload = {
            "idempotency_key": "generic-snap-reject-inv",
            "as_of": "2026-08-01T10:00:00+08:00",
            "balance": "100000.00",
            "currency": "CNY"
        }
        res = self.client.post(
            f"/api/v1/accounts/{self.acc_invest_cny_id}/snapshots",
            headers=self.headers,
            json=payload
        )
        self.assertEqual(res.status_code, 422)
        err = res.json()
        self.assertEqual(err["error"]["code"], "ACCOUNT_TYPE_MISMATCH")

    def test_get_investment_performance_api_success_and_filters(self):
        # Create baseline and two subsequent periods
        self.client.post(
            f"/api/v1/investment-accounts/{self.acc_invest_cny_id}/snapshots",
            headers=self.headers,
            json={
                "idempotency_key": "perf-snap-001",
                "as_of": "2026-06-30T23:59:59+08:00",
                "total_asset_value": "100000.00",
                "currency": "CNY"
            }
        )
        self.client.post(
            f"/api/v1/investment-accounts/{self.acc_invest_cny_id}/snapshots",
            headers=self.headers,
            json={
                "idempotency_key": "perf-snap-002",
                "as_of": "2026-07-31T23:59:59+08:00",
                "total_asset_value": "110000.00",
                "currency": "CNY"
            }
        )
        self.client.post(
            f"/api/v1/investment-accounts/{self.acc_invest_cny_id}/snapshots",
            headers=self.headers,
            json={
                "idempotency_key": "perf-snap-003",
                "as_of": "2026-08-31T23:59:59+08:00",
                "total_asset_value": "125000.00",
                "currency": "CNY"
            }
        )

        # GET performance (all)
        res = self.client.get(
            f"/api/v1/investment-accounts/{self.acc_invest_cny_id}/performance",
            headers=self.headers
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["account_id"], str(self.acc_invest_cny_id))
        self.assertEqual(data["currency"], "CNY")
        self.assertEqual(len(data["periods"]), 2)
        self.assertEqual(data["periods"][0]["pnl_amount"], "10000.00")
        self.assertEqual(data["periods"][1]["pnl_amount"], "15000.00")

        # GET performance with filter from=2026-08-01
        res_filtered = self.client.get(
            f"/api/v1/investment-accounts/{self.acc_invest_cny_id}/performance?from=2026-08-01",
            headers=self.headers
        )
        self.assertEqual(res_filtered.status_code, 200)
        data_f = res_filtered.json()
        self.assertEqual(len(data_f["periods"]), 1)
        self.assertEqual(data_f["periods"][0]["pnl_amount"], "15000.00")


if __name__ == "__main__":
    unittest.main()
