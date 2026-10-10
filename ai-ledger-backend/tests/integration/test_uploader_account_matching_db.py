"""Actual device-token authentication, capture receipts and account ownership."""
import base64
import hashlib
import io
from datetime import date
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image
from app.api.deps import get_db_connection
from app.api.routes.expenses import get_capture_model
from app.api.routes.transactions import get_fx_provider
from app.db import get_connection
from app.main import create_app
from app.repositories import simplified_schema as schema, spending as repo
from app.services.gemini_service import ExpenseExtractionResult, GeminiService, MockGeminiService
from tests.support.db_helper import BaseDbTestCase


class TestUploaderAccountMatchingDb(BaseDbTestCase):
    def seed_test_data(self):
        self.hh = uuid4()
        schema.create_household(self.conn, "Matching Family", household_id=self.hh)
        self.users, self.tokens, self.devices, self.accounts = [], [], [], []
        for name, role in (("First Person", "owner"), ("Second Person", "member")):
            user = uuid4()
            schema.create_user(self.conn, auth_subject=str(uuid4()), email=f"{user}@example.test",
                               display_name=name, user_id=user)
            schema.add_household_member(self.conn, self.hh, user, role)
            token = f"vbl_test_{uuid4().hex}"
            device = schema.create_device(self.conn, self.hh, user, "Phone", "ios",
                                          hashlib.sha256(token.encode()).digest())
            account = schema.create_account(self.conn, self.hh, name + " Wallet", "wallet only", "cash", "CNY",
                                            owner_user_id=user, opened_on=date(2026, 1, 1))
            schema.create_alias(self.conn, self.hh, account["id"], "Wallet")
            self.users.append(user)
            self.tokens.append(token)
            self.devices.append(device)
            self.accounts.append(account)
        schema.create_category(self.conn, self.hh, "Other", "expense", is_fallback=True)
        self.conn.commit()
        self.model = MockGeminiService(self.result())
        self.app = create_app()
        self.client = TestClient(self.app)
        self.app.dependency_overrides[get_capture_model] = lambda: self.model
        self.app.dependency_overrides[get_fx_provider] = lambda: None

        def db():
            conn = get_connection(self.test_schema)
            try:
                yield conn
            finally:
                if not conn.closed:
                    conn.close()
        self.app.dependency_overrides[get_db_connection] = db
        image = io.BytesIO()
        Image.new("RGB", (3, 3), (10, 20, 30)).save(image, format="PNG")
        self.image = base64.b64encode(image.getvalue()).decode()

    def result(self, **changes):
        data = dict(occurred_on=date(2026, 1, 2), merchant="Cafe", original_amount=Decimal("28.50"),
            original_currency="CNY", from_account="Wallet", category="Other", payment_mode="one_off",
            intent="expense", date_evidence="visible", account_candidates=[a["name"] for a in self.accounts],
            field_confidence={k: .99 for k in ("amount", "currency", "date", "intent", "category", "account")})
        data.update(changes)
        return ExpenseExtractionResult(**data)

    def capture(self, user=0, key=None, **changes):
        body = {"idempotency_key": key or str(uuid4()), "captured_at": "2026-01-02T12:00:00+08:00",
                "image": {"mime_type": "image/png", "base64": self.image}, "note": None, **changes}
        return self.client.post("/api/v1/expenses", headers={"Authorization": f"Bearer {self.tokens[user]}"}, json=body)

    def stored_account(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        saved = response.json()
        if saved["status"] == "committed":
            return repo.get(self.conn, self.hh, saved["transaction_id"])["account_id"]
        self.assertEqual(saved["status"], "needs_confirmation")
        return (saved["draft"]["from_account"] or {}).get("id")

    def test_same_image_and_key_resolve_each_real_device_user_without_bypassing_duplicates(self):
        key = str(uuid4())
        with patch.object(self.model, "extract_expense", wraps=self.model.extract_expense) as extract:
            first = self.capture(0, key)
            second = self.capture(1, key)
            self.assertEqual(str(self.stored_account(first)), str(self.accounts[0]["id"]))
            self.assertEqual(str(self.stored_account(second)), str(self.accounts[1]["id"]))
            self.assertEqual(first.json()["status"], "committed")
            self.assertEqual(second.json()["status"], "needs_confirmation")
            self.assertIn("POSSIBLE_DUPLICATE", {w["code"] for w in second.json()["warnings"]})
            for index, call in enumerate(extract.call_args_list):
                accounts = call.args[3]
                owned = [a for a in accounts if a["ownership"] == "uploader"]
                self.assertEqual([a["id"] for a in owned], [self.accounts[index]["id"]])
                prompt = GeminiService(api_key="mock").build_system_prompt(accounts, [])
                for secret in (*self.tokens, *(str(u) for u in self.users), *(a["email"] for a in schema.list_household_members(self.conn, self.hh))):
                    self.assertNotIn(secret, prompt)
            self.assertEqual(self.capture(0, key).json(), first.json())
            self.assertEqual(self.capture(1, key).json(), second.json())
            self.assertEqual(self.model.call_count, 2)
        identity = second.json()["request_id"]
        wrong = self.client.post(f"/api/v1/ingestion-requests/{identity}/confirm",
                                headers={"Authorization": f"Bearer {self.tokens[0]}"})
        self.assertEqual(wrong.status_code, 404)
        confirmed = self.client.post(f"/api/v1/ingestion-requests/{identity}/confirm",
                                    headers={"Authorization": f"Bearer {self.tokens[1]}"})
        self.assertEqual(confirmed.json()["status"], "committed", confirmed.text)
        self.assertEqual(self.stored_account(confirmed), self.accounts[1]["id"])
        self.assertEqual(self.model.call_count, 2)
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) n FROM transactions")[0]["n"], 2)
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) n FROM account_snapshots")[0]["n"], 0)

    def test_explicit_other_member_account_is_not_overridden(self):
        self.model.set_next_result(self.result(from_account=self.accounts[1]["name"],
            account_candidates=[self.accounts[1]["name"]]))
        self.assertEqual(self.stored_account(self.capture(0)), self.accounts[1]["id"])

    def test_multiple_similar_owned_accounts_keep_metadata_review(self):
        schema.create_account(self.conn, self.hh, "First Person Spare Wallet", "spare wallet", "cash", "CNY",
                              owner_user_id=self.users[0], opened_on=date(2026, 1, 1))
        self.conn.commit()
        self.model.set_next_result(self.result(from_account="Mobile wallet",
            account_candidates=[self.accounts[0]["name"], "First Person Spare Wallet", self.accounts[1]["name"]]))
        response = self.capture(0)
        self.assertIsNone(self.stored_account(response))
        self.assertIn("MISSING_ACCOUNT", {w["code"] for w in response.json()["warnings"]})

    def test_unique_unassigned_account_is_still_eligible(self):
        shared = schema.create_account(self.conn, self.hh, "Shared Cash", "shared cash", "cash", "CNY",
                                       opened_on=date(2026, 1, 1))
        schema.create_alias(self.conn, self.hh, shared["id"], "Shared Wallet")
        self.conn.commit()
        self.model.set_next_result(self.result(from_account="Shared Wallet", account_candidates=[shared["name"]]))
        self.assertEqual(self.stored_account(self.capture(0)), shared["id"])

    def test_no_evidence_or_low_confidence_cannot_default_to_owned_account(self):
        for changes in ({"from_account": None, "account_candidates": []},
                        {"from_account": "Unknown account", "account_candidates": None},
                        {"from_account": self.accounts[0]["name"], "account_candidates": [self.accounts[0]["name"]],
                         "field_confidence": {"account": .4}}):
            with self.subTest(changes=changes):
                self.model.set_next_result(self.result(**changes))
                response = self.capture(0)
                self.assertIsNone(self.stored_account(response))
                self.assertIn("MISSING_ACCOUNT", {w["code"] for w in response.json()["warnings"]})

    def test_invalid_uploader_never_reaches_model_or_reserves_capture(self):
        self.tokens[0] = "vbl_test_not_registered"
        self.assertEqual(self.capture(0).status_code, 401)
        with self.conn.cursor() as cur:
            cur.execute("UPDATE users SET status='disabled' WHERE id=%s", (self.users[1],))
        self.conn.commit()
        self.assertEqual(self.capture(1).status_code, 403)
        self.assertEqual(self.model.call_count, 0)
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) n FROM ingestion_requests")[0]["n"], 0)
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) n FROM transactions")[0]["n"], 0)

    def test_replay_keeps_history_after_ownership_changes(self):
        key = str(uuid4())
        first = self.capture(0, key)
        self.assertEqual(self.stored_account(first), self.accounts[0]["id"])
        with self.conn.cursor() as cur:
            cur.execute("SELECT to_jsonb(t) FROM transactions t ORDER BY id")
            original = cur.fetchall()
            cur.execute("UPDATE accounts SET owner_user_id=%s WHERE household_id=%s", (self.users[1], self.hh))
        self.conn.commit()
        self.assertEqual(self.capture(0, key).json(), first.json())
        self.assertEqual(self.model.call_count, 1)
        with self.conn.cursor() as cur:
            cur.execute("SELECT to_jsonb(t) FROM transactions t ORDER BY id")
            self.assertEqual(cur.fetchall(), original)
