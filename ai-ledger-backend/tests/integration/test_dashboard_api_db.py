import unittest
from uuid import UUID, uuid4
import hashlib
from decimal import Decimal
from datetime import date, datetime, timezone, timedelta
from fastapi.testclient import TestClient

from app.db import get_connection, transaction
from app.main import create_app
from app.api.deps import get_db_connection
from app.api.routes.dashboard import router as dashboard_router
from app.repositories import accounts as accounts_repo
from app.repositories import categories as categories_repo
from app.repositories import transactions as tx_repo
from app.repositories import installments as installments_repo
from app.repositories import devices as devices_repo
from app.services.reference_fx_service import ReferenceFxService

try:
    from tests.support.db_helper import BaseDbTestCase
except ModuleNotFoundError:
    from support.db_helper import BaseDbTestCase

class TestDashboardApiDb(BaseDbTestCase):
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

    def seed_test_data(self):
        self.household_id = uuid4()
        self.user_id = uuid4()
        self.device_id = uuid4()
        self.raw_token = f"vbl_test_{uuid4().hex}"
        self.token_hash = hashlib.sha256(self.raw_token.encode("utf-8")).digest()
        self.headers = {"Authorization": f"Bearer {self.raw_token}"}

        # Inject fixed ReferenceFxService into dashboard router for deterministic tests
        self.mock_fx = ReferenceFxService(fixed_rates={
            ("USD", "CNY"): Decimal("7.200000000000"),
            ("EUR", "CNY"): Decimal("7.800000000000")
        })
        dashboard_router._reference_fx_service = self.mock_fx

        conn = get_connection(self.test_schema)
        # Household B for cross-household isolation tests
        self.household_b_id = uuid4()
        self.user_b_id = uuid4()
        self.device_b_id = uuid4()
        self.raw_token_b = f"vbl_test_{uuid4().hex}"
        self.token_b_hash = hashlib.sha256(self.raw_token_b.encode("utf-8")).digest()
        self.headers_b = {"Authorization": f"Bearer {self.raw_token_b}"}

        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                accounts_repo.create_household(conn, self.household_id, "Dashboard Household", date(2026, 1, 1), "CNY")
                accounts_repo.create_user(conn, self.user_id, "auth_dash_d", "User D", "user_d@dash.local", "CNY")
                accounts_repo.add_user_to_household(conn, self.household_id, self.user_id, role="owner")
                devices_repo.create_device(conn, self.device_id, self.user_id, "Device D", self.token_hash)

                # Setup Household B
                accounts_repo.create_household(conn, self.household_b_id, "Household B", date(2026, 1, 1), "CNY")
                accounts_repo.create_user(conn, self.user_b_id, "auth_dash_b", "User B", "user_b@dash.local", "CNY")
                accounts_repo.add_user_to_household(conn, self.household_b_id, self.user_b_id, role="owner")
                devices_repo.create_device(conn, self.device_b_id, self.user_b_id, "Device B", self.token_b_hash)

                # Seed accounts
                self.acc_cash_id = uuid4()
                accounts_repo.create_account(
                    conn, self.acc_cash_id, self.household_id, "ICBC Checking", "cash", "CNY"
                )
                self.acc_credit_id = uuid4()
                accounts_repo.create_account(
                    conn, self.acc_credit_id, self.household_id, "CMB Credit", "credit", "CNY", billing_day=5, due_day=25
                )
                self.acc_credit_no_snap_id = uuid4()
                accounts_repo.create_account(
                    conn, self.acc_credit_no_snap_id, self.household_id, "BOC Credit No Snap", "credit", "CNY", billing_day=10, due_day=30
                )
                self.acc_usd_id = uuid4()
                accounts_repo.create_account(
                    conn, self.acc_usd_id, self.household_id, "Chase USD", "savings", "USD"
                )

                # Seed balances and snapshots in account_state
                # Cash: 50,000 CNY, snapshot 5 days ago
                # Credit: -8,000 CNY (debt), snapshot 45 days ago
                # USD savings: 2,000 USD (2000 * 7.20 = 14,400 CNY), snapshot 120 days ago
                now_utc = datetime.now(timezone.utc)
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE account_state
                        SET ledger_balance = 50000.00, last_authoritative_snapshot_at = %s
                        WHERE account_id = %s;
                        """,
                        (now_utc - timedelta(days=5), self.acc_cash_id)
                    )
                    cur.execute(
                        """
                        UPDATE account_state
                        SET ledger_balance = -8000.00, last_authoritative_snapshot_at = %s
                        WHERE account_id = %s;
                        """,
                        (now_utc - timedelta(days=45), self.acc_credit_id)
                    )
                    cur.execute(
                        """
                        UPDATE account_state
                        SET ledger_balance = 2000.00, last_authoritative_snapshot_at = %s
                        WHERE account_id = %s;
                        """,
                        (now_utc - timedelta(days=120), self.acc_usd_id)
                    )

                    # Seed credit card snapshot on CMB Credit
                    cur.execute(
                        """
                        INSERT INTO credit_card_snapshots (
                            id, household_id, account_id, as_of,
                            statement_period_start, statement_period_end,
                            statement_balance, remaining_statement_due,
                            unbilled_balance, current_outstanding,
                            currency, source, created_at
                        ) VALUES (
                            gen_random_uuid(), %s, %s, %s,
                            %s, %s,
                            12000.00, 8000.00,
                            2500.00, 10500.00,
                            'CNY', 'statement', now()
                        );
                        """,
                        (
                            self.household_id, self.acc_credit_id, now_utc,
                            date(2026, 7, 6), date(2026, 8, 5)
                        )
                    )

                    # Seed multi-currency investment pnl periods:
                    # Row 1: 100.00 CNY
                    # Row 2: 100.00 USD (at rate 7.20 -> 720.00 CNY)
                    # Expected total_pnl: 100 + 720 = 820.00 CNY
                    snap_open_id = uuid4()
                    snap_close_id = uuid4()
                    cur.execute(
                        """
                        INSERT INTO account_snapshots (id, household_id, account_id, as_of, balance, currency, snapshot_type, source)
                        VALUES (%s, %s, %s, %s, 100000.00, 'CNY', 'investment_valuation', 'statement'),
                               (%s, %s, %s, %s, 100100.00, 'CNY', 'investment_valuation', 'statement');
                        """,
                        (
                            snap_open_id, self.household_id, self.acc_cash_id, now_utc - timedelta(days=30),
                            snap_close_id, self.household_id, self.acc_cash_id, now_utc
                        )
                    )
                    cur.execute(
                        """
                        INSERT INTO investment_pnl_periods (
                            id, household_id, account_id, opening_snapshot_id, closing_snapshot_id,
                            period_start, period_end, contributions_amount, withdrawals_amount,
                            pnl_amount, currency, status
                        ) VALUES (
                            gen_random_uuid(), %s, %s, %s, %s,
                            %s, %s, 0.00, 0.00,
                            100.00, 'CNY', 'confirmed'
                        ), (
                            gen_random_uuid(), %s, %s, %s, %s,
                            %s, %s, 0.00, 0.00,
                            100.00, 'USD', 'confirmed'
                        );
                        """,
                        (
                            self.household_id, self.acc_cash_id, snap_open_id, snap_close_id,
                            date(2026, 8, 1), date(2026, 8, 30),
                            self.household_id, self.acc_usd_id, snap_open_id, snap_close_id,
                            date(2026, 8, 1), date(2026, 8, 30)
                        )
                    )

                # Seed Cash Flow transactions:
                # 1000 expense, 20 fee, 100 refund, 5000 cash_income
                # Plus 2000 transfer (excluded)
                self.cat_exp_id = uuid4()
                categories_repo.create_category(conn, self.cat_exp_id, self.household_id, "Shopping", "expense")
                self.cat_inc_id = uuid4()
                categories_repo.create_category(conn, self.cat_inc_id, self.household_id, "Job", "income")

                tx_repo.create_transaction(
                    conn, uuid4(), self.household_id, "expense", date(2026, 8, 10),
                    original_amount=Decimal("1000.00"), original_currency="CNY",
                    from_amount=Decimal("1000.00"), from_currency="CNY",
                    from_account_id=self.acc_cash_id, category_id=self.cat_exp_id,
                    status="committed"
                )
                tx_repo.create_transaction(
                    conn, uuid4(), self.household_id, "fee", date(2026, 8, 11),
                    original_amount=Decimal("20.00"), original_currency="CNY",
                    from_amount=Decimal("20.00"), from_currency="CNY",
                    from_account_id=self.acc_cash_id,
                    status="committed"
                )
                tx_repo.create_transaction(
                    conn, uuid4(), self.household_id, "refund", date(2026, 8, 12),
                    original_amount=Decimal("100.00"), original_currency="CNY",
                    to_amount=Decimal("100.00"), to_currency="CNY",
                    to_account_id=self.acc_cash_id, category_id=self.cat_exp_id,
                    status="committed"
                )
                tx_repo.create_transaction(
                    conn, uuid4(), self.household_id, "cash_income", date(2026, 8, 15),
                    original_amount=Decimal("5000.00"), original_currency="CNY",
                    to_amount=Decimal("5000.00"), to_currency="CNY",
                    to_account_id=self.acc_cash_id, category_id=self.cat_inc_id,
                    status="committed"
                )
                tx_repo.create_transaction(
                    conn, uuid4(), self.household_id, "transfer", date(2026, 8, 18),
                    original_amount=Decimal("2000.00"), original_currency="CNY",
                    from_amount=Decimal("2000.00"), from_currency="CNY",
                    to_amount=Decimal("2000.00"), to_currency="CNY",
                    from_account_id=self.acc_cash_id, to_account_id=self.acc_credit_id,
                    status="committed"
                )

                # Seed installment plan
                self.plan_id = uuid4()
                installments_repo.create_installment_plan(
                    conn,
                    plan_id=self.plan_id,
                    household_id=self.household_id,
                    credit_account_id=self.acc_credit_id,
                    purchase_occurred_on=date(2026, 8, 1),
                    merchant="Apple Store",
                    original_amount=Decimal("12000.00"),
                    original_currency="CNY",
                    account_principal_amount=Decimal("12000.00"),
                    account_currency="CNY",
                    total_periods=12,
                    first_statement_month=date(2026, 9, 1)
                )
                installments_repo.create_installment_period(
                    conn,
                    period_id=uuid4(),
                    plan_id=self.plan_id,
                    period_no=1,
                    scheduled_amount=Decimal("1000.00"),
                    currency="CNY",
                    recognition_month=date(2026, 9, 1),
                    status="scheduled"
                )
                installments_repo.create_installment_period(
                    conn,
                    period_id=uuid4(),
                    plan_id=self.plan_id,
                    period_no=2,
                    scheduled_amount=Decimal("1000.00"),
                    currency="CNY",
                    recognition_month=date(2026, 10, 1),
                    status="scheduled"
                )

        finally:
            conn.close()

    def test_dashboard_overview_endpoint(self):
        res = self.client.get("/api/v1/dashboard/overview", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["reporting_currency"], "CNY")

        # Total Assets: 50,000 (CNY Cash) + 14,400 (2000 USD * 7.20) = 64400.00
        # Total Liabilities: 8,000 (CMB Credit) = 8000.00
        # Net Worth: 64400 - 8000 = 56400.00
        self.assertEqual(data["total_assets"], "64400.00")
        self.assertEqual(data["total_liabilities"], "8000.00")
        self.assertEqual(data["net_worth"], "56400.00")

        # Freshness:
        # Accounts: 4 total (ICBC Checking, CMB Credit, BOC Credit No Snap, Chase USD)
        # <= 30d: ICBC Checking (5d) -> 1/4 = 0.2500
        # <= 90d: ICBC Checking (5d), CMB Credit (45d) -> 2/4 = 0.5000
        self.assertEqual(data["data_freshness"]["confirmed_within_30d_ratio"], "0.2500")
        self.assertEqual(data["data_freshness"]["confirmed_within_90d_ratio"], "0.5000")

    def test_dashboard_cash_flow_endpoint(self):
        res = self.client.get("/api/v1/dashboard/cash-flow?from=2026-08-01&to=2026-08-31", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()

        # Expected:
        # cash_income: 5000.00
        # expense: 1000 + 20 - 100 = 920.00
        # refund: 100.00
        # net_cash_flow: 5000 - 920 = 4080.00
        self.assertEqual(data["cash_income"], "5000.00")
        self.assertEqual(data["expense"], "920.00")
        self.assertEqual(data["refund"], "100.00")
        self.assertEqual(data["net_cash_flow"], "4080.00")
        self.assertEqual(data["reporting_currency"], "CNY")

    def test_dashboard_investments_endpoint(self):
        res = self.client.get("/api/v1/dashboard/investments?from=2026-08-01&to=2026-08-31", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["reporting_currency"], "CNY")
        # 100.00 CNY + 100.00 USD * 7.20 = 820.00 CNY
        self.assertEqual(data["total_pnl"], "820.00")
        self.assertEqual(len(data["items"]), 2)

        items_by_curr = {it["currency"]: it for it in data["items"]}
        self.assertEqual(items_by_curr["CNY"]["pnl_amount"], "100.00")
        self.assertEqual(items_by_curr["USD"]["pnl_amount"], "100.00")

    def test_dashboard_account_freshness_endpoint(self):
        res = self.client.get("/api/v1/dashboard/account-freshness", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        items = {it["account_name"]: it for it in data["items"]}

        self.assertEqual(items["ICBC Checking"]["freshness"], "fresh")
        self.assertLessEqual(items["ICBC Checking"]["age_days"], 6)

        self.assertEqual(items["CMB Credit"]["freshness"], "stale")
        self.assertGreaterEqual(items["CMB Credit"]["age_days"], 40)

        self.assertEqual(items["Chase USD"]["freshness"], "expired")
        self.assertGreaterEqual(items["Chase USD"]["age_days"], 115)

    def test_credit_card_state_endpoint(self):
        # 1. Successful state retrieval for credit card account with snapshot
        res = self.client.get(f"/api/v1/credit-cards/{self.acc_credit_id}/state", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["account_id"], str(self.acc_credit_id))
        self.assertEqual(data["currency"], "CNY")
        self.assertIsNotNone(data["latest_snapshot"])
        self.assertEqual(data["latest_snapshot"]["statement_balance"], "12000.00")
        self.assertEqual(data["latest_snapshot"]["remaining_statement_due"], "8000.00")
        self.assertEqual(data["latest_snapshot"]["unbilled_balance"], "2500.00")
        self.assertEqual(data["latest_snapshot"]["current_outstanding"], "10500.00")

        # 2. Credit card with NO snapshot returns latest_snapshot=null
        res_no_snap = self.client.get(f"/api/v1/credit-cards/{self.acc_credit_no_snap_id}/state", headers=self.headers)
        self.assertEqual(res_no_snap.status_code, 200)
        data_no_snap = res_no_snap.json()
        self.assertEqual(data_no_snap["account_id"], str(self.acc_credit_no_snap_id))
        self.assertIsNone(data_no_snap["latest_snapshot"])

        # 3. Cross-household isolation: Household B device cannot read Household A card state -> 404
        res_iso = self.client.get(f"/api/v1/credit-cards/{self.acc_credit_id}/state", headers=self.headers_b)
        self.assertEqual(res_iso.status_code, 404)
        self.assertEqual(res_iso.json()["error"]["code"], "ACCOUNT_NOT_FOUND")

        # 4. Reject non-credit accounts -> 422
        res_bad = self.client.get(f"/api/v1/credit-cards/{self.acc_cash_id}/state", headers=self.headers)
        self.assertEqual(res_bad.status_code, 422)
        self.assertEqual(res_bad.json()["error"]["code"], "ACCOUNT_TYPE_MISMATCH")

    def test_installments_read_endpoints(self):
        # Count transactions before
        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM transactions WHERE household_id = %s;", (self.household_id,))
                tx_count_before = cur.fetchone()[0]
        finally:
            conn.close()

        # 1. List installment plans
        res_list = self.client.get("/api/v1/installments", headers=self.headers)
        self.assertEqual(res_list.status_code, 200)
        items = res_list.json()["items"]
        self.assertGreaterEqual(len(items), 1)
        self.assertEqual(items[0]["merchant"], "Apple Store")
        self.assertEqual(items[0]["total_periods"], 12)
        self.assertEqual(items[0]["original_amount"], "12000.00")

        # 2. Get installment plan details
        res_plan = self.client.get(f"/api/v1/installments/{self.plan_id}", headers=self.headers)
        self.assertEqual(res_plan.status_code, 200)
        plan_data = res_plan.json()
        self.assertEqual(plan_data["id"], str(self.plan_id))
        self.assertEqual(plan_data["merchant"], "Apple Store")
        self.assertEqual(len(plan_data["periods"]), 2)
        self.assertEqual(plan_data["periods"][0]["period_no"], 1)
        self.assertEqual(plan_data["periods"][0]["scheduled_amount"], "1000.00")
        self.assertEqual(plan_data["periods"][0]["status"], "scheduled")

        # 3. Cross-household isolation: Household B device cannot read Household A installment plan -> 404
        res_iso_plan = self.client.get(f"/api/v1/installments/{self.plan_id}", headers=self.headers_b)
        self.assertEqual(res_iso_plan.status_code, 404)
        self.assertEqual(res_iso_plan.json()["error"]["code"], "INSTALLMENT_PLAN_NOT_FOUND")

        # 4. Verify no new transactions were created by the read API
        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM transactions WHERE household_id = %s;", (self.household_id,))
                tx_count_after = cur.fetchone()[0]
                self.assertEqual(tx_count_before, tx_count_after)
        finally:
            conn.close()

if __name__ == "__main__":
    unittest.main()

