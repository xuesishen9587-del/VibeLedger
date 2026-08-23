import unittest
from uuid import UUID, uuid4
import hashlib
from decimal import Decimal
from datetime import date, datetime, timezone
from fastapi.testclient import TestClient

from app.db import get_connection, transaction
from app.main import create_app
from app.api.deps import get_db_connection
from app.repositories import accounts as accounts_repo
from app.repositories import devices as devices_repo
from app.repositories import transactions as tx_repo
from app.repositories import audit as audit_repo

try:
    from tests.support.db_helper import BaseDbTestCase
except ModuleNotFoundError:
    from support.db_helper import BaseDbTestCase

class TestAccountsApiDb(BaseDbTestCase):
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

        # Household B for isolation tests
        self.household_b_id = uuid4()
        self.user_b_id = uuid4()
        self.device_b_id = uuid4()
        self.raw_token_b = f"vbl_test_{uuid4().hex}"
        self.token_b_hash = hashlib.sha256(self.raw_token_b.encode("utf-8")).digest()
        self.headers_b = {"Authorization": f"Bearer {self.raw_token_b}"}

        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                # Setup Household A
                accounts_repo.create_household(conn, self.household_id, "Test Household A", date(2026, 1, 1), "CNY")
                accounts_repo.create_user(conn, self.user_id, "auth_user_a", "User A", "user_a@test.local", "CNY")
                accounts_repo.add_user_to_household(conn, self.household_id, self.user_id, role="owner")
                devices_repo.create_device(conn, self.device_id, self.user_id, "iPhone A", self.token_hash)

                # Setup Household B
                accounts_repo.create_household(conn, self.household_b_id, "Test Household B", date(2026, 1, 1), "USD")
                accounts_repo.create_user(conn, self.user_b_id, "auth_user_b", "User B", "user_b@test.local", "USD")
                accounts_repo.add_user_to_household(conn, self.household_b_id, self.user_b_id, role="owner")
                devices_repo.create_device(conn, self.device_b_id, self.user_b_id, "iPhone B", self.token_b_hash)


        finally:
            conn.close()

    def test_create_account_atomicity_and_audit(self):
        payload = {
            "name": "ICBC Salary",
            "institution": "ICBC",
            "account_type": "cash",
            "currency": "CNY",
            "owner_user_id": str(self.user_id)
        }
        res = self.client.post("/api/v1/accounts", json=payload, headers=self.headers)
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(data["name"], "ICBC Salary")
        self.assertEqual(data["account_type"], "cash")
        self.assertEqual(data["currency"], "CNY")
        self.assertEqual(data["status"], "active")
        self.assertEqual(data["row_version"], 0)
        self.assertEqual(data["state"]["ledger_balance"], "0.00")
        self.assertIsNone(data["state"]["last_authoritative_snapshot_at"])

        account_id = UUID(data["id"])

        # Check DB rows
        conn = get_connection(self.test_schema)
        try:
            acc = accounts_repo.get_account_with_state(conn, account_id, self.household_id)
            self.assertIsNotNone(acc)
            self.assertEqual(acc["ledger_balance"], Decimal("0"))

            # Verify audit event
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT action, entity_type, entity_id, actor_type, after_data
                    FROM audit_events
                    WHERE household_id = %s AND entity_id = %s;
                    """,
                    (self.household_id, account_id)
                )
                audit_row = cur.fetchone()
                self.assertIsNotNone(audit_row)
                self.assertEqual(audit_row[0], "create")
                self.assertEqual(audit_row[1], "account")
                self.assertEqual(audit_row[3], "device")
        finally:
            conn.close()

    def test_create_account_validations(self):
        # 1. Non-credit account with billing_day rejected
        res = self.client.post("/api/v1/accounts", json={
            "name": "Bad Cash",
            "account_type": "cash",
            "currency": "CNY",
            "billing_day": 10
        }, headers=self.headers)
        self.assertEqual(res.status_code, 422)

        # 2. Owner user ID not in household rejected
        foreign_user_id = uuid4()
        res = self.client.post("/api/v1/accounts", json={
            "name": "Foreign User Acc",
            "account_type": "cash",
            "currency": "CNY",
            "owner_user_id": str(foreign_user_id)
        }, headers=self.headers)
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json()["error"]["code"], "USER_NOT_IN_HOUSEHOLD")

        # 3. Duplicate active name in same household rejected
        res_ok = self.client.post("/api/v1/accounts", json={
            "name": "CMB Visa",
            "account_type": "credit",
            "currency": "USD",
            "billing_day": 5,
            "due_day": 25
        }, headers=self.headers)
        self.assertEqual(res_ok.status_code, 201)

        res_dup = self.client.post("/api/v1/accounts", json={
            "name": "cmb visa", # case insensitive duplicate
            "account_type": "credit",
            "currency": "USD"
        }, headers=self.headers)
        self.assertEqual(res_dup.status_code, 422)
        self.assertEqual(res_dup.json()["error"]["code"], "ACCOUNT_NAME_CONFLICT")

    def test_list_accounts_with_filters_and_state(self):
        # Create cash and credit accounts
        res1 = self.client.post("/api/v1/accounts", json={
            "name": "Savings A",
            "account_type": "savings",
            "currency": "CNY"
        }, headers=self.headers)
        self.assertEqual(res1.status_code, 201)

        res2 = self.client.post("/api/v1/accounts", json={
            "name": "Credit A",
            "account_type": "credit",
            "currency": "CNY",
            "billing_day": 1,
            "due_day": 20
        }, headers=self.headers)
        self.assertEqual(res2.status_code, 201)

        # Query all
        res_all = self.client.get("/api/v1/accounts", headers=self.headers)
        self.assertEqual(res_all.status_code, 200)
        items = res_all.json()["items"]
        self.assertGreaterEqual(len(items), 2)

        # Filter by account_type=credit
        res_credit = self.client.get("/api/v1/accounts?account_type=credit", headers=self.headers)
        self.assertEqual(res_credit.status_code, 200)
        c_items = res_credit.json()["items"]
        for it in c_items:
            self.assertEqual(it["account_type"], "credit")

    def test_patch_account_optimistic_concurrency_and_audit(self):
        # Create account
        res = self.client.post("/api/v1/accounts", json={
            "name": "DBS Multi",
            "institution": "DBS",
            "account_type": "cash",
            "currency": "SGD"
        }, headers=self.headers)
        self.assertEqual(res.status_code, 201)
        data = res.json()
        acc_id = data["id"]
        row_version = data["row_version"]

        # Patch with stale row_version -> 409 Conflict
        res_stale = self.client.patch(f"/api/v1/accounts/{acc_id}", json={
            "name": "DBS Renamed",
            "row_version": row_version + 5
        }, headers=self.headers)
        self.assertEqual(res_stale.status_code, 409)
        self.assertEqual(res_stale.json()["error"]["code"], "ROW_VERSION_CONFLICT")

        # Patch with correct row_version -> 200 OK
        res_patch = self.client.patch(f"/api/v1/accounts/{acc_id}", json={
            "name": "DBS Main SGD",
            "institution": "DBS Bank Ltd",
            "row_version": row_version
        }, headers=self.headers)
        self.assertEqual(res_patch.status_code, 200)
        p_data = res_patch.json()
        self.assertEqual(p_data["name"], "DBS Main SGD")
        self.assertEqual(p_data["institution"], "DBS Bank Ltd")
        self.assertEqual(p_data["row_version"], row_version + 1)

    def test_patch_account_immutability_rules(self):
        res = self.client.post("/api/v1/accounts", json={
            "name": "Immutable Test Acc",
            "account_type": "cash",
            "currency": "USD"
        }, headers=self.headers)
        acc_id = UUID(res.json()["id"])
        row_version = res.json()["row_version"]

        # 1. Before financial history, currency CAN be updated
        res_curr_ok = self.client.patch(f"/api/v1/accounts/{acc_id}", json={
            "currency": "EUR",
            "row_version": row_version
        }, headers=self.headers)
        self.assertEqual(res_curr_ok.status_code, 200)
        self.assertEqual(res_curr_ok.json()["currency"], "EUR")
        self.assertEqual(res_curr_ok.json()["row_version"], row_version + 1)
        row_version = res_curr_ok.json()["row_version"]

        # Confirm DB has EUR
        conn = get_connection(self.test_schema)
        try:
            acc_db = accounts_repo.get_account(conn, acc_id)
            self.assertEqual(acc_db["currency"], "EUR")
        finally:
            conn.close()

        # 2. Record a transaction on this account
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                tx_id = uuid4()
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=tx_id,
                    household_id=self.household_id,
                    transaction_type="cash_income",
                    occurred_on=date(2026, 8, 1),
                    original_amount=Decimal("100.00"),
                    original_currency="EUR",
                    to_amount=Decimal("100.00"),
                    to_currency="EUR",
                    to_account_id=acc_id,
                    source="shortcut",
                    status="committed"
                )
        finally:
            conn.close()

        # 3. Try to change currency after transaction exists -> 422 CURRENCY_IMMUTABLE
        res_curr = self.client.patch(f"/api/v1/accounts/{acc_id}", json={
            "currency": "GBP",
            "row_version": row_version
        }, headers=self.headers)
        self.assertEqual(res_curr.status_code, 422)
        self.assertEqual(res_curr.json()["error"]["code"], "CURRENCY_IMMUTABLE")

        # 4. Try to change account_type after transaction exists -> 422 ACCOUNT_TYPE_IMMUTABLE
        res_type = self.client.patch(f"/api/v1/accounts/{acc_id}", json={
            "account_type": "credit",
            "row_version": row_version
        }, headers=self.headers)
        self.assertEqual(res_type.status_code, 422)
        self.assertEqual(res_type.json()["error"]["code"], "ACCOUNT_TYPE_IMMUTABLE")

    def test_patch_account_nullable_fields_clearing_and_credit_validation(self):
        # 1. Create linked cash account
        res_cash = self.client.post("/api/v1/accounts", json={
            "name": "Auto Debit Cash",
            "account_type": "cash",
            "currency": "CNY"
        }, headers=self.headers)
        cash_id = res_cash.json()["id"]

        # 2. Create credit account with all nullable fields populated
        res_credit = self.client.post("/api/v1/accounts", json={
            "name": "Full Credit Acc",
            "institution": "Chase Bank",
            "account_type": "credit",
            "currency": "USD",
            "owner_user_id": str(self.user_id),
            "linked_cash_account_id": cash_id,
            "billing_day": 5,
            "due_day": 25
        }, headers=self.headers)
        self.assertEqual(res_credit.status_code, 201)
        cr_data = res_credit.json()
        cr_id = cr_data["id"]
        row_v = cr_data["row_version"]
        self.assertEqual(cr_data["institution"], "Chase Bank")
        self.assertEqual(cr_data["billing_day"], 5)
        self.assertEqual(cr_data["due_day"], 25)
        self.assertEqual(cr_data["linked_cash_account_id"], cash_id)

        # 3. Explicitly clear nullable fields with null
        res_clear = self.client.patch(f"/api/v1/accounts/{cr_id}", json={
            "institution": None,
            "owner_user_id": None,
            "linked_cash_account_id": None,
            "billing_day": None,
            "due_day": None,
            "row_version": row_v
        }, headers=self.headers)
        self.assertEqual(res_clear.status_code, 200)
        c_data = res_clear.json()
        self.assertIsNone(c_data["institution"])
        self.assertIsNone(c_data["owner_user_id"])
        self.assertIsNone(c_data["linked_cash_account_id"])
        self.assertIsNone(c_data["billing_day"])
        self.assertIsNone(c_data["due_day"])
        row_v = c_data["row_version"]

        # 4. Inconsistent credit/non-credit billing state produces canonical 422
        # Create credit account with billing_day=5, due_day=25
        res_cr2 = self.client.post("/api/v1/accounts", json={
            "name": "Credit To Cash Test",
            "account_type": "credit",
            "currency": "USD",
            "billing_day": 5,
            "due_day": 25
        }, headers=self.headers)
        cr2_id = res_cr2.json()["id"]
        cr2_row_v = res_cr2.json()["row_version"]

        # PATCH account_type="cash" without clearing billing_day/due_day -> 422
        res_bad_type = self.client.patch(f"/api/v1/accounts/{cr2_id}", json={
            "account_type": "cash",
            "row_version": cr2_row_v
        }, headers=self.headers)
        self.assertEqual(res_bad_type.status_code, 422)
        self.assertEqual(res_bad_type.json()["error"]["code"], "LINKED_ACCOUNT_INVALID")

        # PATCH account_type="cash" WITH clearing billing_day/due_day -> 200 OK
        res_ok_type = self.client.patch(f"/api/v1/accounts/{cr2_id}", json={
            "account_type": "cash",
            "billing_day": None,
            "due_day": None,
            "row_version": cr2_row_v
        }, headers=self.headers)
        self.assertEqual(res_ok_type.status_code, 200)
        self.assertEqual(res_ok_type.json()["account_type"], "cash")
        self.assertIsNone(res_ok_type.json()["billing_day"])
        self.assertIsNone(res_ok_type.json()["due_day"])


    def test_deactivate_account_soft_delete(self):
        res = self.client.post("/api/v1/accounts", json={
            "name": "To Deactivate",
            "account_type": "cash",
            "currency": "CNY"
        }, headers=self.headers)
        acc_id = res.json()["id"]

        res_deact = self.client.post(f"/api/v1/accounts/{acc_id}/deactivate", headers=self.headers)
        self.assertEqual(res_deact.status_code, 200)
        self.assertEqual(res_deact.json()["status"], "inactive")

        # Confirm filtered list shows it as inactive
        res_active = self.client.get("/api/v1/accounts?status=active", headers=self.headers)
        active_ids = [a["id"] for a in res_active.json()["items"]]
        self.assertNotIn(acc_id, active_ids)

        res_inactive = self.client.get("/api/v1/accounts?status=inactive", headers=self.headers)
        inactive_ids = [a["id"] for a in res_inactive.json()["items"]]
        self.assertIn(acc_id, inactive_ids)

    def test_account_aliases_crud_and_conflict(self):
        res_acc1 = self.client.post("/api/v1/accounts", json={
            "name": "ICBC Card 1",
            "account_type": "credit",
            "currency": "CNY"
        }, headers=self.headers)
        acc1_id = res_acc1.json()["id"]

        res_acc2 = self.client.post("/api/v1/accounts", json={
            "name": "ICBC Card 2",
            "account_type": "credit",
            "currency": "CNY"
        }, headers=self.headers)
        acc2_id = res_acc2.json()["id"]

        # Create alias on acc1
        res_a1 = self.client.post(f"/api/v1/accounts/{acc1_id}/aliases", json={"alias": "工行"}, headers=self.headers)
        self.assertEqual(res_a1.status_code, 201)
        alias1_id = res_a1.json()["id"]
        self.assertEqual(res_a1.json()["alias"], "工行")

        # Duplicate alias on same account -> 422 conflict
        res_dup = self.client.post(f"/api/v1/accounts/{acc1_id}/aliases", json={"alias": "工行"}, headers=self.headers)
        self.assertEqual(res_dup.status_code, 422)
        self.assertEqual(res_dup.json()["error"]["code"], "ACCOUNT_ALIAS_CONFLICT")

        # Same alias on different account in same household -> allowed (201)
        res_a2 = self.client.post(f"/api/v1/accounts/{acc2_id}/aliases", json={"alias": "工行"}, headers=self.headers)
        self.assertEqual(res_a2.status_code, 201)

        # List aliases for acc1
        res_list = self.client.get(f"/api/v1/accounts/{acc1_id}/aliases", headers=self.headers)
        self.assertEqual(res_list.status_code, 200)
        self.assertEqual(len(res_list.json()["items"]), 1)

        # Delete alias on acc1
        res_del = self.client.delete(f"/api/v1/accounts/{acc1_id}/aliases/{alias1_id}", headers=self.headers)
        self.assertEqual(res_del.status_code, 200)
        self.assertEqual(res_del.json()["status"], "deactivated")

        # Verify acc1 aliases empty
        res_list2 = self.client.get(f"/api/v1/accounts/{acc1_id}/aliases", headers=self.headers)
        self.assertEqual(len(res_list2.json()["items"]), 0)




    def test_cross_household_isolation(self):
        # Create account in Household A
        res_a = self.client.post("/api/v1/accounts", json={
            "name": "Household A Secret Acc",
            "account_type": "cash",
            "currency": "CNY"
        }, headers=self.headers)
        acc_a_id = res_a.json()["id"]

        # Household B device attempts to patch Household A's account -> 404 Not Found
        res_hack = self.client.patch(f"/api/v1/accounts/{acc_a_id}", json={
            "name": "Hacked Name",
            "row_version": 0
        }, headers=self.headers_b)
        self.assertEqual(res_hack.status_code, 404)
        self.assertEqual(res_hack.json()["error"]["code"], "ACCOUNT_NOT_FOUND")

        # Household B device attempts to deactivate Household A's account -> 404 Not Found
        res_deact = self.client.post(f"/api/v1/accounts/{acc_a_id}/deactivate", headers=self.headers_b)
        self.assertEqual(res_deact.status_code, 404)

        # Household B device attempts to read aliases -> 404 Not Found
        res_alias = self.client.get(f"/api/v1/accounts/{acc_a_id}/aliases", headers=self.headers_b)
        self.assertEqual(res_alias.status_code, 404)

if __name__ == "__main__":
    unittest.main()
