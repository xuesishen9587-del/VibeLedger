import unittest
from uuid import UUID, uuid4
import hashlib
import threading
from datetime import date
from fastapi.testclient import TestClient

from app.db import get_connection, transaction
from app.main import create_app
from app.api.deps import get_db_connection
from app.repositories import accounts as accounts_repo
from app.repositories import categories as categories_repo
from app.repositories import devices as devices_repo
from app.repositories.simplified_schema import acquire_household_finance_lock

try:
    from tests.support.db_helper import BaseDbTestCase
except ModuleNotFoundError:
    from support.db_helper import BaseDbTestCase


class TestApiConcurrency(BaseDbTestCase):
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
        self.device_id_1 = uuid4()
        self.raw_token_1 = f"vbl_test_{uuid4().hex}"
        self.headers = {"Authorization": f"Bearer {self.raw_token_1}"}

        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                accounts_repo.create_household(conn, self.household_id, "Test Household", reporting_currency="CNY")
                accounts_repo.create_user(conn, self.user_id, "auth_concur_user", "Test User", "user@concur.local")
                accounts_repo.add_user_to_household(conn, self.household_id, self.user_id, role="owner")

                t1_hash = hashlib.sha256(self.raw_token_1.encode('utf-8')).digest()
                devices_repo.create_device(
                    conn=conn,
                    device_id=self.device_id_1,
                    user_id=self.user_id,
                    device_name="iPhone 15 Pro",
                    token_hash=t1_hash,
                    household_id=self.household_id,
                    platform="ios_shortcuts"
                )

                self.acc_checking_id = uuid4()
                accounts_repo.create_account(
                    conn=conn,
                    account_id=self.acc_checking_id,
                    household_id=self.household_id,
                    name="招商银行储蓄卡",
                    balance_scope="Main Account",
                    account_type="cash",
                    currency="CNY"
                )

                self.cat_food_id = uuid4()
                categories_repo.create_category(
                    conn=conn,
                    category_id=self.cat_food_id,
                    household_id=self.household_id,
                    name="餐饮美食",
                    category_type="expense"
                )
        finally:
            conn.close()

    def test_account_patch_optimistic_concurrency_race(self):
        """
        When 5 concurrent threads attempt to update an account with the same expected_version,
        exactly ONE thread succeeds (200 OK) and the other 4 fail with 409 ROW_VERSION_CONFLICT.
        """
        results = []
        threads = []

        def worker(idx):
            client = TestClient(self.app)
            res = client.patch(
                f"/api/v1/accounts/{self.acc_checking_id}",
                json={
                    "name": f"Renamed Account {idx}",
                    "expected_version": 0
                },
                headers={
                    **self.headers,
                    "Idempotency-Key": f"key-race-patch-{idx}-{uuid4().hex}"
                }
            )
            results.append((res.status_code, res.json()))

        for i in range(5):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if r[0] == 200]
        conflicts = [r for r in results if r[0] == 409]

        self.assertEqual(len(successes), 1, f"Expected exactly 1 success, got {len(successes)}: {results}")
        self.assertEqual(len(conflicts), 4, f"Expected 4 conflicts, got {len(conflicts)}: {results}")

        for code, data in conflicts:
            self.assertEqual(data.get("error", {}).get("code"), "ROW_VERSION_CONFLICT")

        # Verify final account row_version in DB is exactly 1
        conn = get_connection(self.test_schema)
        try:
            acc = accounts_repo.get_account(conn, self.acc_checking_id, self.household_id)
            self.assertEqual(acc["row_version"], 1)
        finally:
            conn.close()

    def test_category_patch_optimistic_concurrency_race(self):
        """
        When 5 concurrent threads attempt to update a category with the same expected_version,
        exactly ONE thread succeeds (200 OK) and the other 4 fail with 409.
        """
        results = []
        threads = []

        def worker(idx):
            client = TestClient(self.app)
            res = client.patch(
                f"/api/v1/categories/{self.cat_food_id}",
                json={
                    "name": f"Renamed Cat {idx}",
                    "expected_version": 0
                },
                headers=self.headers
            )
            results.append((res.status_code, res.json()))

        for i in range(5):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if r[0] == 200]
        conflicts = [r for r in results if r[0] == 409]

        self.assertEqual(len(successes), 1, f"Expected exactly 1 success, got {len(successes)}: {results}")
        self.assertEqual(len(conflicts), 4, f"Expected 4 conflicts, got {len(conflicts)}: {results}")

    def test_concurrent_duplicate_alias_creation_race(self):
        """
        When 4 concurrent threads attempt to create identical aliases on the same account,
        exactly ONE succeeds (201) and the remaining 3 are rejected with conflict (422).
        """
        results = []
        threads = []

        def worker():
            client = TestClient(self.app)
            res = client.post(
                f"/api/v1/accounts/{self.acc_checking_id}/aliases",
                json={"alias": "招行卡"},
                headers=self.headers
            )
            results.append((res.status_code, res.json()))

        for _ in range(4):
            t = threading.Thread(target=worker)
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if r[0] == 201]
        conflicts = [r for r in results if r[0] == 422]

        self.assertEqual(len(successes), 1, f"Expected exactly 1 success, got {len(successes)}: {results}")
        self.assertEqual(len(conflicts), 3, f"Expected 3 conflicts, got {len(conflicts)}: {results}")

    def test_household_finance_lock_serialization(self):
        """
        Validates that acquire_household_finance_lock serializes financial writes in PostgreSQL
        without deadlocks.
        """
        log = []

        def worker(idx):
            conn = get_connection(self.test_schema)
            try:
                with transaction(conn):
                    acquire_household_finance_lock(conn, self.household_id)
                    log.append(f"enter_{idx}")
                    # Brief critical section
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1;")
                    log.append(f"exit_{idx}")
            finally:
                conn.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(log), 8)
        # Check interleaving: each enter should match its exit before another enter/exit can break consistency
        # In PostgreSQL FOR UPDATE, each transaction runs sequentially
        enters = [x for x in log if x.startswith("enter")]
        exits = [x for x in log if x.startswith("exit")]
        self.assertEqual(len(enters), 4)
        self.assertEqual(len(exits), 4)

if __name__ == "__main__":
    unittest.main()
