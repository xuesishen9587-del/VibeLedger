import unittest
from uuid import uuid4
from decimal import Decimal
from datetime import date, datetime, timezone
from fastapi.testclient import TestClient

from app.db import get_connection
from app.main import create_app
from app.api.deps import get_db_connection
from app.auth.browser_verifier import set_browser_verifier, StaticBrowserAuthVerifier
from app.repositories import users as users_repo
from app.repositories import household_members as members_repo
from app.repositories import accounts as accounts_repo
from app.repositories import categories as categories_repo
from app.repositories import transactions as transactions_repo
import app.repositories.transactions as tx_repo
import app.services.ledger_service as ledger_service
from tests.support.db_helper import BaseDbTestCase


class TestTransactionCorrectionsApiDb(BaseDbTestCase):
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
        cls.static_verifier = StaticBrowserAuthVerifier()
        set_browser_verifier(cls.static_verifier)

    @classmethod
    def tearDownClass(cls):
        set_browser_verifier(None)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.household_id = uuid4()
        members_repo.create_household(
            self.conn,
            household_id=self.household_id,
            name="Correction Test Household",
            reporting_currency="CNY",
            ledger_start_date=date(2026, 1, 1),
            status="active"
        )
        self.user_id = uuid4()
        self.auth_subject = "auth0|corr_user"
        users_repo.create_user(
            self.conn,
            user_id=self.user_id,
            auth_subject=self.auth_subject,
            display_name="Corr User",
            email="corr@example.com",
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
            name="Wallet",
            institution="Cash",
            account_type="cash",
            currency="CNY",
            owner_user_id=self.user_id
        )
        self.account_jpy_id = uuid4()
        accounts_repo.create_account(
            self.conn,
            account_id=self.account_jpy_id,
            household_id=self.household_id,
            name="JPY Cash",
            institution="Cash",
            account_type="cash",
            currency="JPY",
            owner_user_id=self.user_id
        )
        self.category_id = uuid4()
        categories_repo.create_category(
            self.conn,
            category_id=self.category_id,
            household_id=self.household_id,
            name="Dining",
            category_type="expense"
        )
        self.inactive_category_id = uuid4()
        categories_repo.create_category(
            self.conn,
            category_id=self.inactive_category_id,
            household_id=self.household_id,
            name="Old Cat",
            category_type="expense",
            status="inactive"
        )
        self.income_category_id = uuid4()
        categories_repo.create_category(
            self.conn,
            category_id=self.income_category_id,
            household_id=self.household_id,
            name="Salary",
            category_type="income"
        )
        self.browser_token = "corr.jwt.token"
        self.static_verifier.register_token(
            self.browser_token,
            {"sub": self.auth_subject, "exp": 9999999999}
        )
        self.conn.commit()

    def test_void_transaction_workflow_and_concurrency(self):
        # 1. Record an expense
        tx = ledger_service.record_expense(
            self.conn,
            household_id=self.household_id,
            from_account_id=self.account_id,
            amount=Decimal("150.00"),
            currency="CNY",
            category_id=self.category_id,
            occurred_on=date(2026, 8, 1),
            merchant="Restaurant",
            created_by_user_id=self.user_id
        )
        self.conn.commit()
        tx_id = tx["id"]
        headers = {"Authorization": f"Bearer {self.browser_token}"}

        # 2. Missing expected_version -> 422
        resp_no_ver = self.client.post(
            f"/api/v1/transactions/{tx_id}/void",
            headers=headers,
            json={"delete_reason": "No version"}
        )
        self.assertEqual(resp_no_ver.status_code, 422)

        # 3. Wrong expected_version -> 409
        resp_wrong_ver = self.client.post(
            f"/api/v1/transactions/{tx_id}/void",
            headers=headers,
            json={"expected_version": 999, "delete_reason": "Wrong version"}
        )
        self.assertEqual(resp_wrong_ver.status_code, 409)

        # 4. Correct expected_version -> 200
        resp = self.client.post(
            f"/api/v1/transactions/{tx_id}/void",
            headers=headers,
            json={"expected_version": 0, "delete_reason": "Wrong entry"}
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "voided")
        self.assertTrue(data["account_balance_restored"])

        # 5. Check balance restored
        state = accounts_repo.get_account_state(self.conn, self.account_id)
        self.assertEqual(state["ledger_balance"], Decimal("0.000000"))

        # 6. Second void on same transaction -> 409
        resp_double = self.client.post(
            f"/api/v1/transactions/{tx_id}/void",
            headers=headers,
            json={"expected_version": 1, "delete_reason": "Double void"}
        )
        self.assertEqual(resp_double.status_code, 409)

    def test_correction_preview_and_commit_workflow(self):
        tx = ledger_service.record_expense(
            self.conn,
            household_id=self.household_id,
            from_account_id=self.account_id,
            amount=Decimal("100.00"),
            currency="CNY",
            category_id=self.category_id,
            occurred_on=date(2026, 8, 1),
            merchant="Store A",
            created_by_user_id=self.user_id
        )
        self.conn.commit()
        tx_id = tx["id"]
        headers = {"Authorization": f"Bearer {self.browser_token}"}

        # Preview correction
        preview_resp = self.client.post(
            f"/api/v1/transactions/{tx_id}/corrections/preview",
            headers=headers,
            json={"merchant": "Store B", "from_amount": "120.00"}
        )
        self.assertEqual(preview_resp.status_code, 200)
        prev_data = preview_resp.json()
        self.assertEqual(prev_data["expected_version"], 0)
        self.assertEqual(len(prev_data["account_state_deltas"]), 1)
        self.assertEqual(prev_data["account_state_deltas"][0]["delta"], "-20.00")

        # Commit correction with optimistic concurrency
        commit_resp = self.client.post(
            f"/api/v1/transactions/{tx_id}/corrections/commit",
            headers=headers,
            json={
                "expected_version": 0,
                "changes": {"merchant": "Store B", "from_amount": "120.00"},
                "reason": "Corrected receipt total"
            }
        )
        self.assertEqual(commit_resp.status_code, 200)
        updated = commit_resp.json()
        self.assertEqual(updated["merchant"], "Store B")
        self.assertEqual(updated["from_amount"], "120.00")
        self.assertEqual(updated["row_version"], 1)

        # Attempt commit with old expected_version -> 409 Conflict
        conflict_resp = self.client.post(
            f"/api/v1/transactions/{tx_id}/corrections/commit",
            headers=headers,
            json={
                "expected_version": 0,
                "changes": {"merchant": "Store C"},
                "reason": "Stale edit"
            }
        )
        self.assertEqual(conflict_resp.status_code, 409)
        self.assertEqual(conflict_resp.json()["error"]["code"], "ROW_VERSION_CONFLICT")

    def test_correction_validation_and_domain_hardening(self):
        tx = ledger_service.record_expense(
            self.conn,
            household_id=self.household_id,
            from_account_id=self.account_id,
            amount=Decimal("100.00"),
            currency="CNY",
            category_id=self.category_id,
            occurred_on=date(2026, 8, 1),
            merchant="Store A",
            created_by_user_id=self.user_id
        )
        self.conn.commit()
        tx_id = tx["id"]
        headers = {"Authorization": f"Bearer {self.browser_token}"}

        # 1. Negative amount -> 422
        resp_neg = self.client.post(
            f"/api/v1/transactions/{tx_id}/corrections/commit",
            headers=headers,
            json={"expected_version": 0, "changes": {"from_amount": "-50.00"}}
        )
        self.assertEqual(resp_neg.status_code, 422)

        # 2. Zero amount -> 422
        resp_zero = self.client.post(
            f"/api/v1/transactions/{tx_id}/corrections/commit",
            headers=headers,
            json={"expected_version": 0, "changes": {"from_amount": "0.00"}}
        )
        self.assertEqual(resp_zero.status_code, 422)

        # 3. Unsupported extra field -> 422
        resp_extra = self.client.post(
            f"/api/v1/transactions/{tx_id}/corrections/commit",
            headers=headers,
            json={"expected_version": 0, "changes": {"arbitrary_sql": "DROP TABLE transactions"}}
        )
        self.assertEqual(resp_extra.status_code, 422)

        # 4. Inactive category -> 422
        resp_inactive_cat = self.client.post(
            f"/api/v1/transactions/{tx_id}/corrections/commit",
            headers=headers,
            json={"expected_version": 0, "changes": {"category_id": str(self.inactive_category_id)}}
        )
        self.assertEqual(resp_inactive_cat.status_code, 422)

        # 5. Income category on expense transaction -> 422
        resp_cat_mismatch = self.client.post(
            f"/api/v1/transactions/{tx_id}/corrections/commit",
            headers=headers,
            json={"expected_version": 0, "changes": {"category_id": str(self.income_category_id)}}
        )
        self.assertEqual(resp_cat_mismatch.status_code, 422)

    def test_jpy_minor_unit_precision_validation(self):
        tx_jpy = ledger_service.record_expense(
            self.conn,
            household_id=self.household_id,
            from_account_id=self.account_jpy_id,
            amount=Decimal("1000"),
            currency="JPY",
            category_id=self.category_id,
            occurred_on=date(2026, 8, 1),
            merchant="Tokyo Store",
            created_by_user_id=self.user_id
        )
        self.conn.commit()
        tx_id = tx_jpy["id"]
        headers = {"Authorization": f"Bearer {self.browser_token}"}

        # JPY cannot have decimal fractions (.50) -> 422
        resp_jpy_frac = self.client.post(
            f"/api/v1/transactions/{tx_id}/corrections/commit",
            headers=headers,
            json={"expected_version": 0, "changes": {"from_amount": "1000.50"}}
        )
        self.assertEqual(resp_jpy_frac.status_code, 422)

    def test_transaction_shape_invariants_and_cross_field_reprojection(self):
        headers = {"Authorization": f"Bearer {self.browser_token}"}

        # 1. Expense cannot have to_amount
        exp_tx = ledger_service.record_expense(
            self.conn,
            household_id=self.household_id,
            from_account_id=self.account_id,
            amount=Decimal("100.00"),
            currency="CNY",
            category_id=self.category_id,
            occurred_on=date(2026, 8, 2),
            merchant="Cafe",
            created_by_user_id=self.user_id
        )
        self.conn.commit()
        resp_exp_bad = self.client.post(
            f"/api/v1/transactions/{exp_tx['id']}/corrections/commit",
            headers=headers,
            json={"expected_version": 0, "changes": {"to_amount": "100.00"}}
        )
        self.assertEqual(resp_exp_bad.status_code, 422)

        # 2. Income cannot have from_amount
        inc_tx = ledger_service.record_cash_income(
            self.conn,
            household_id=self.household_id,
            to_account_id=self.account_id,
            amount=Decimal("5000.00"),
            currency="CNY",
            category_id=self.income_category_id,
            occurred_on=date(2026, 8, 3),
            merchant="Employer",
            created_by_user_id=self.user_id
        )
        self.conn.commit()
        resp_inc_bad = self.client.post(
            f"/api/v1/transactions/{inc_tx['id']}/corrections/commit",
            headers=headers,
            json={"expected_version": 0, "changes": {"from_amount": "5000.00"}}
        )
        self.assertEqual(resp_inc_bad.status_code, 422)

        # 3. Successful same-currency expense correction updates original_amount, from_amount, reporting_amount
        resp_exp_ok = self.client.post(
            f"/api/v1/transactions/{exp_tx['id']}/corrections/commit",
            headers=headers,
            json={"expected_version": 0, "changes": {"from_amount": "125.00", "merchant": "Specialty Cafe"}}
        )
        self.assertEqual(resp_exp_ok.status_code, 200)
        detail = resp_exp_ok.json()
        self.assertEqual(detail["from_amount"], "125.00")
        self.assertEqual(detail["original_amount"], "125.00")
        self.assertEqual(detail["reporting_amount"], "125.00")
        self.assertEqual(detail["merchant"], "Specialty Cafe")
        self.assertEqual(detail["row_version"], 1)

        # 4. Same-currency transfer maintains equality and FX=1.0
        acc_b_id = uuid4()
        accounts_repo.create_account(
            self.conn,
            account_id=acc_b_id,
            household_id=self.household_id,
            name="Bank B",
            institution="B",
            account_type="cash",
            currency="CNY",
            owner_user_id=self.user_id
        )
        self.conn.commit()
        transfer_tx = ledger_service.record_transfer(
            self.conn,
            household_id=self.household_id,
            from_account_id=self.account_id,
            to_account_id=acc_b_id,
            from_amount=Decimal("300.00"),
            to_amount=Decimal("300.00"),
            from_currency="CNY",
            to_currency="CNY",
            occurred_on=date(2026, 8, 4),
            created_by_user_id=self.user_id
        )
        self.conn.commit()

        # Mismatched amounts on same currency transfer -> 422
        resp_trans_bad = self.client.post(
            f"/api/v1/transactions/{transfer_tx['id']}/corrections/commit",
            headers=headers,
            json={"expected_version": 0, "changes": {"from_amount": "400.00", "to_amount": "450.00"}}
        )
        self.assertEqual(resp_trans_bad.status_code, 422)

        # Updating one leg updates both and effective_fx_rate=1.0
        resp_trans_ok = self.client.post(
            f"/api/v1/transactions/{transfer_tx['id']}/corrections/commit",
            headers=headers,
            json={"expected_version": 0, "changes": {"from_amount": "500.00"}}
        )
        self.assertEqual(resp_trans_ok.status_code, 200)
        trans_detail = resp_trans_ok.json()
        self.assertEqual(trans_detail["from_amount"], "500.00")
        self.assertEqual(trans_detail["to_amount"], "500.00")
        self.assertEqual(Decimal(str(trans_detail["effective_fx_rate"])), Decimal("1.000000000000"))

        # 5. System adjustment transactions reject amount corrections
        adj_tx = ledger_service.record_reconciliation_adjustment(
            self.conn,
            household_id=self.household_id,
            account_id=self.account_id,
            amount=Decimal("50.00"),
            currency="CNY",
            occurred_on=date(2026, 8, 5)
        )
        self.conn.commit()
        resp_adj_bad = self.client.post(
            f"/api/v1/transactions/{adj_tx['id']}/corrections/commit",
            headers=headers,
            json={"expected_version": 0, "changes": {"from_amount": "80.00"}}
        )
        self.assertEqual(resp_adj_bad.status_code, 422)

    def test_foreign_card_reporting_fx_frozen_invariants(self):
        """
        Proves that correcting a statement-confirmed multi-currency transaction
        (original_currency EUR != account_currency USD != reporting_currency CNY):
        - preserves the frozen reporting_fx_rate
        - recomputes reporting_amount deterministically using the frozen rate
        - preserves foreign original_amount/original_currency
        - atomically updates account_state
        - records audit events
        """
        headers = {"Authorization": f"Bearer {self.browser_token}"}

        # 1. Create USD account with initial balance
        acc_usd_id = uuid4()
        accounts_repo.create_account(
            self.conn,
            account_id=acc_usd_id,
            household_id=self.household_id,
            name="Chase USD Card",
            institution="Chase",
            account_type="credit",
            currency="USD",
            owner_user_id=self.user_id
        )
        accounts_repo.update_account_state_projection(self.conn, acc_usd_id, Decimal("1000.00"))

        # 2. Insert statement-confirmed foreign transaction
        # Billed: 68.90 USD, Original: 60.00 EUR, Frozen USD->CNY FX rate: 7.200000000000 -> 496.08 CNY
        foreign_tx_id = uuid4()
        now_dt = datetime.now(timezone.utc)
        tx_repo.create_transaction(
            self.conn,
            tx_id=foreign_tx_id,
            household_id=self.household_id,
            transaction_type="expense",
            occurred_on=date(2026, 8, 10),
            original_amount=Decimal("60.00"),
            original_currency="EUR",
            from_amount=Decimal("68.90"),
            from_currency="USD",
            from_account_id=acc_usd_id,
            category_id=self.category_id,
            merchant="Paris Hotel",
            reporting_amount=Decimal("496.08"),
            reporting_currency="CNY",
            reporting_fx_rate=Decimal("7.200000000000"),
            reporting_fx_locked_at=now_dt,
            verification_status="statement_confirmed"
        )
        self.conn.commit()

        # 3. Correct from_amount to 68.20 USD
        resp_corr = self.client.post(
            f"/api/v1/transactions/{foreign_tx_id}/corrections/commit",
            headers=headers,
            json={
                "expected_version": 0,
                "changes": {"from_amount": "68.20"},
                "reason": "Adjusted statement fee refund"
            }
        )
        self.assertEqual(resp_corr.status_code, 200)
        res_data = resp_corr.json()

        # Assert from_amount updated
        self.assertEqual(res_data["from_amount"], "68.20")
        # Assert EUR original amount preserved (foreign card semantics)
        self.assertEqual(res_data["original_amount"], "60.00")
        self.assertEqual(res_data["original_currency"], "EUR")
        # Assert frozen reporting_fx_rate preserved
        self.assertEqual(Decimal(str(res_data["reporting_fx_rate"])), Decimal("7.200000000000"))
        # Assert reporting_amount recomputed from 68.20 * 7.20 = 491.04 CNY
        self.assertEqual(res_data["reporting_amount"], "491.04")
        self.assertEqual(res_data["reporting_currency"], "CNY")
        self.assertEqual(res_data["row_version"], 1)

        # 4. Assert account_state updated atomically (1000.00 + (68.90 - 68.20) = 1000.70)
        state = accounts_repo.get_account_state(self.conn, acc_usd_id)
        self.assertEqual(Decimal(str(state["ledger_balance"])), Decimal("1000.70"))
