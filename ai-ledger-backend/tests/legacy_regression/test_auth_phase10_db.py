import unittest
import hashlib
import io
import pypdf
from uuid import uuid4, UUID
from decimal import Decimal
from datetime import date
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.db import get_connection
from app.main import create_app
from app.api.deps import get_db_connection
from app.auth.browser_verifier import set_browser_verifier, StaticBrowserAuthVerifier
from app.repositories import users as users_repo
from app.repositories import household_members as members_repo
from app.repositories import devices as devices_repo
from app.repositories import accounts as accounts_repo
from app.services.statement_parser import MockStatementParser
from app.domain.statements import StatementExtractionResult
from app.api.routes.statements import router as statements_router
from tests.support.db_helper import BaseDbTestCase


def _make_pdf_bytes(text_lines: list[str]) -> bytes:
    writer = pypdf.PdfWriter()
    from pypdf.generic import DecodedStreamObject, NameObject, DictionaryObject
    for t in text_lines:
        page = writer.add_blank_page(width=612, height=792)
        stream = DecodedStreamObject()
        stream_content = f"BT /F1 12 Tf 72 712 Td ({t}) Tj ET".encode("latin-1", errors="replace")
        stream.set_data(stream_content)
        
        resources = DictionaryObject()
        fonts = DictionaryObject()
        f1 = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        fonts[NameObject("/F1")] = f1
        resources[NameObject("/Font")] = fonts
        
        page[NameObject("/Resources")] = resources
        page[NameObject("/Contents")] = stream

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class TestAuthPhase10Db(BaseDbTestCase):
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

    def seed_test_data(self):
        # 1. Household 1 (active)
        self.household_id = uuid4()
        members_repo.create_household(
            self.conn,
            household_id=self.household_id,
            name="Household One",
            reporting_currency="CNY",
            ledger_start_date=date(2026, 1, 1),
            status="active"
        )

        # 2. Active User 1
        self.user_id = uuid4()
        self.auth_subject = "auth0|user_1"
        users_repo.create_user(
            self.conn,
            user_id=self.user_id,
            auth_subject=self.auth_subject,
            display_name="User One",
            email="user1@example.com",
            default_currency="CNY",
            status="active"
        )
        members_repo.add_household_member(
            self.conn,
            household_id=self.household_id,
            user_id=self.user_id,
            role="owner"
        )

        # 3. Active Device for User 1
        self.raw_device_token = "device_secret_token_1234567890_abcdef"
        self.token_hash = hashlib.sha256(self.raw_device_token.encode("utf-8")).digest()
        self.device_id = uuid4()
        devices_repo.create_device(
            self.conn,
            device_id=self.device_id,
            user_id=self.user_id,
            device_name="iPhone 15 Pro",
            token_hash=self.token_hash,
            platform="ios_shortcuts",
            status="active"
        )

        # Register JWT token in static verifier (3-segment header.payload.signature)
        self.browser_jwt_token = "valid.jwt.user1"
        self.static_verifier.register_token(
            self.browser_jwt_token,
            {"sub": self.auth_subject, "email": "user1@example.com"}
        )
        self.conn.commit()

    def test_device_auth_success(self):
        # Calling an authenticated endpoint (e.g. GET /api/v1/accounts)
        res = self.client.get(
            "/api/v1/accounts",
            headers={"Authorization": f"Bearer {self.raw_device_token}"}
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("items", res.json())

    def test_browser_jwt_auth_success(self):
        res = self.client.get(
            "/api/v1/accounts",
            headers={"Authorization": f"Bearer {self.browser_jwt_token}"}
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("items", res.json())

    def test_missing_auth_header_fails_401(self):
        res = self.client.get("/api/v1/accounts")
        self.assertEqual(res.status_code, 401)
        data = res.json()
        self.assertEqual(data["error"]["code"], "UNAUTHORIZED")
        self.assertFalse(data["error"]["retryable"])

    def test_empty_bearer_token_fails_401(self):
        res = self.client.get("/api/v1/accounts", headers={"Authorization": "Bearer   "})
        self.assertEqual(res.status_code, 401)
        data = res.json()
        self.assertEqual(data["error"]["code"], "UNAUTHORIZED")

    def test_invalid_device_token_fails_401(self):
        res = self.client.get(
            "/api/v1/accounts",
            headers={"Authorization": "Bearer invalid_nonexistent_token_string"}
        )
        self.assertEqual(res.status_code, 401)
        data = res.json()
        self.assertEqual(data["error"]["code"], "UNAUTHORIZED")

    def test_revoked_device_token_fails_401(self):
        # Revoke device in DB
        devices_repo.revoke_device(self.conn, self.device_id)
        self.conn.commit()

        res = self.client.get(
            "/api/v1/accounts",
            headers={"Authorization": f"Bearer {self.raw_device_token}"}
        )
        self.assertEqual(res.status_code, 401)
        data = res.json()
        self.assertEqual(data["error"]["code"], "DEVICE_REVOKED")

    def test_disabled_user_fails_403(self):
        # Disable user
        with self.conn.cursor() as cur:
            cur.execute("UPDATE users SET status = 'disabled' WHERE id = %s;", (self.user_id,))
        self.conn.commit()

        # Test device auth with disabled user
        res = self.client.get(
            "/api/v1/accounts",
            headers={"Authorization": f"Bearer {self.raw_device_token}"}
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "USER_DISABLED")

        # Test browser JWT auth with disabled user
        res_jwt = self.client.get(
            "/api/v1/accounts",
            headers={"Authorization": f"Bearer {self.browser_jwt_token}"}
        )
        self.assertEqual(res_jwt.status_code, 403)
        self.assertEqual(res_jwt.json()["error"]["code"], "USER_DISABLED")

    def test_user_zero_household_memberships_fails_403(self):
        # Remove membership
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM household_members WHERE user_id = %s;", (self.user_id,))
        self.conn.commit()

        res = self.client.get(
            "/api/v1/accounts",
            headers={"Authorization": f"Bearer {self.raw_device_token}"}
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "USER_NOT_IN_HOUSEHOLD")

    def test_user_multiple_household_memberships_fails_closed_403(self):
        # Create second active household and add user
        hid2 = uuid4()
        members_repo.create_household(
            self.conn,
            household_id=hid2,
            name="Household Two",
            status="active"
        )
        members_repo.add_household_member(
            self.conn,
            household_id=hid2,
            user_id=self.user_id,
            role="member"
        )
        self.conn.commit()

        res = self.client.get(
            "/api/v1/accounts",
            headers={"Authorization": f"Bearer {self.raw_device_token}"}
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "AMBIGUOUS_HOUSEHOLD_MEMBERSHIP")

    def test_jwt_unregistered_sub_fails_401(self):
        token = "valid.jwt.unknown.user"
        self.static_verifier.register_token(token, {"sub": "auth0|unknown_unregistered_sub"})

        res = self.client.get(
            "/api/v1/accounts",
            headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"]["code"], "UNAUTHORIZED")

    def test_invalid_3segment_jwt_does_not_fallback_to_device_auth(self):
        # Pre-seed a device whose token_hash matches the SHA-256 of "fake.jwt.token"
        fake_jwt = "fake.jwt.token"
        fake_jwt_hash = hashlib.sha256(fake_jwt.encode("utf-8")).digest()
        dev_id = uuid4()
        devices_repo.create_device(
            self.conn,
            device_id=dev_id,
            user_id=self.user_id,
            device_name="Confused Deputy Target Device",
            token_hash=fake_jwt_hash,
            platform="ios_shortcuts",
            status="active"
        )
        self.conn.commit()

        # Sending 3-segment token that fails JWT verification must NOT authenticate as device
        res = self.client.get(
            "/api/v1/accounts",
            headers={"Authorization": f"Bearer {fake_jwt}"}
        )
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"]["code"], "UNAUTHORIZED")

    def test_browser_jwt_on_device_only_expense_fails_403(self):
        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.browser_jwt_token}"},
            json={
                "idempotency_key": "test_idemp_key_12345",
                "captured_at": "2026-02-15T12:00:00+08:00",
                "image": {"mime_type": "image/jpeg", "base64": "aW1hZ2VkYXRh"}
            }
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "FORBIDDEN")

    def test_browser_jwt_on_device_only_ingestion_confirm_fails_403(self):
        res = self.client.post(
            f"/api/v1/ingestion-requests/{uuid4()}/confirm",
            headers={"Authorization": f"Bearer {self.browser_jwt_token}"}
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "FORBIDDEN")

    def test_device_token_on_post_devices_fails_403(self):
        res = self.client.post(
            "/api/v1/devices",
            headers={"Authorization": f"Bearer {self.raw_device_token}"},
            json={"device_name": "New Device", "platform": "ios_shortcuts"}
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "FORBIDDEN")

    def test_two_users_same_household_acceptance(self):
        # Create User Member in same Household
        member_id = uuid4()
        member_sub = "auth0|user_member_same_household"
        users_repo.create_user(
            self.conn,
            user_id=member_id,
            auth_subject=member_sub,
            display_name="User Member",
            email="member@example.com",
            default_currency="CNY",
            status="active"
        )
        members_repo.add_household_member(
            self.conn,
            household_id=self.household_id,
            user_id=member_id,
            role="member"
        )

        member_jwt = "valid.jwt.member"
        self.static_verifier.register_token(member_jwt, {"sub": member_sub, "email": "member@example.com"})

        # Create an account in the household
        acc_id = uuid4()
        accounts_repo.create_account(
            self.conn,
            account_id=acc_id,
            household_id=self.household_id,
            name="Joint Checking",
            account_type="cash",
            currency="CNY",
            owner_user_id=self.user_id,
            status="active"
        )
        self.conn.commit()

        # Both Owner and Member can read accounts for Household 1
        res_owner = self.client.get("/api/v1/accounts", headers={"Authorization": f"Bearer {self.browser_jwt_token}"})
        self.assertEqual(res_owner.status_code, 200)
        items_owner = res_owner.json()["items"]
        self.assertTrue(any(a["id"] == str(acc_id) for a in items_owner))

        res_member = self.client.get("/api/v1/accounts", headers={"Authorization": f"Bearer {member_jwt}"})
        self.assertEqual(res_member.status_code, 200)
        items_member = res_member.json()["items"]
        self.assertTrue(any(a["id"] == str(acc_id) for a in items_member))

        # Both can read dashboard overview
        res_dash_owner = self.client.get("/api/v1/dashboard/overview", headers={"Authorization": f"Bearer {self.browser_jwt_token}"})
        self.assertEqual(res_dash_owner.status_code, 200)

        res_dash_member = self.client.get("/api/v1/dashboard/overview", headers={"Authorization": f"Bearer {member_jwt}"})
        self.assertEqual(res_dash_member.status_code, 200)

    def test_browser_snapshot_optional_idempotency_creates_zero_ingestion_rows(self):
        acc_id = uuid4()
        accounts_repo.create_account(
            self.conn,
            account_id=acc_id,
            household_id=self.household_id,
            name="Browser Snapshot Account",
            account_type="cash",
            currency="CNY",
            owner_user_id=self.user_id,
            status="active"
        )
        self.conn.commit()

        # Browser snapshot without idempotency_key
        res = self.client.post(
            f"/api/v1/accounts/{acc_id}/snapshots",
            headers={"Authorization": f"Bearer {self.browser_jwt_token}"},
            json={
                "as_of": "2026-02-15T12:00:00+08:00",
                "balance": "1000.00",
                "currency": "CNY",
                "source": "dashboard_manual"
            }
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "committed")

        # Verify ZERO ingestion_requests rows were inserted
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM ingestion_requests;")
            cnt = cur.fetchone()[0]
        self.assertEqual(cnt, 0)

    def test_device_snapshot_missing_idempotency_rejected(self):
        acc_id = uuid4()
        accounts_repo.create_account(
            self.conn,
            account_id=acc_id,
            household_id=self.household_id,
            name="Device Snapshot Account",
            account_type="cash",
            currency="CNY",
            owner_user_id=self.user_id,
            status="active"
        )
        self.conn.commit()

        # Device snapshot without idempotency_key -> rejected by service logic
        res = self.client.post(
            f"/api/v1/accounts/{acc_id}/snapshots",
            headers={"Authorization": f"Bearer {self.raw_device_token}"},
            json={
                "as_of": "2026-02-15T12:00:00+08:00",
                "balance": "1000.00",
                "currency": "CNY",
                "source": "dashboard_manual"
            }
        )
        self.assertEqual(res.status_code, 422)

    def test_browser_investment_snapshot_optional_idempotency(self):
        acc_id = uuid4()
        accounts_repo.create_account(
            self.conn,
            account_id=acc_id,
            household_id=self.household_id,
            name="Investment Portfolio",
            account_type="investment",
            currency="CNY",
            owner_user_id=self.user_id,
            status="active"
        )
        self.conn.commit()

        # Browser investment snapshot without idempotency_key
        res = self.client.post(
            f"/api/v1/investment-accounts/{acc_id}/snapshots",
            headers={"Authorization": f"Bearer {self.browser_jwt_token}"},
            json={
                "as_of": "2026-02-15T12:00:00+08:00",
                "total_asset_value": "50000.00",
                "currency": "CNY",
                "source": "dashboard_manual"
            }
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["status"], "committed")

        # Verify ZERO ingestion_requests rows
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM ingestion_requests;")
            cnt = cur.fetchone()[0]
        self.assertEqual(cnt, 0)

    def test_device_token_collision_retry(self):
        # Pre-seed existing device token
        colliding_token = "colliding_token_secret_12345678901234"
        colliding_hash = hashlib.sha256(colliding_token.encode("utf-8")).digest()
        devices_repo.create_device(
            self.conn,
            device_id=uuid4(),
            user_id=self.user_id,
            device_name="Existing Colliding Device",
            token_hash=colliding_hash,
            platform="ios_shortcuts",
            status="active"
        )
        self.conn.commit()

        # Mock secrets.token_urlsafe to return colliding_token first, then fresh_token
        fresh_token = "fresh_unique_token_secret_999888777666"
        with patch("secrets.token_urlsafe", side_effect=[colliding_token, fresh_token]):
            dev_dict, raw_tok = devices_repo.create_device_with_token(
                self.conn,
                user_id=self.user_id,
                device_name="Retried Device",
                platform="ios_shortcuts"
            )
            self.conn.commit()

        self.assertEqual(raw_tok, fresh_token)
        self.assertEqual(dev_dict["device_name"], "Retried Device")

    def test_autonomous_last_seen_does_not_commit_or_abort_endpoint_transaction(self):
        # Update device last seen via isolated telemetry
        devices_repo.update_device_last_seen_isolated(self.device_id, schema=self.test_schema)
        dev = devices_repo.get_device_by_id(self.conn, self.device_id)
        self.assertIsNotNone(dev["last_seen_at"])

    def test_audit_provenance_browser_account_mutation(self):
        # 1. Create account via Browser JWT
        res = self.client.post(
            "/api/v1/accounts",
            headers={"Authorization": f"Bearer {self.browser_jwt_token}"},
            json={
                "name": "Browser Checking",
                "account_type": "cash",
                "currency": "CNY"
            }
        )
        self.assertEqual(res.status_code, 201)
        account_id = UUID(res.json()["id"])

        # Verify audit_events for create
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT actor_type, actor_user_id, actor_device_id, action
                FROM audit_events
                WHERE entity_type = 'account' AND entity_id = %s AND action = 'create';
                """,
                (account_id,)
            )
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "user")
            self.assertEqual(row[1], self.user_id)
            self.assertIsNone(row[2])

        # 2. Patch account via Browser JWT
        patch_res = self.client.patch(
            f"/api/v1/accounts/{account_id}",
            headers={"Authorization": f"Bearer {self.browser_jwt_token}"},
            json={
                "name": "Browser Checking Renamed",
                "row_version": res.json()["row_version"]
            }
        )
        self.assertEqual(patch_res.status_code, 200)

        # Verify audit_events for update
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT actor_type, actor_user_id, actor_device_id, action
                FROM audit_events
                WHERE entity_type = 'account' AND entity_id = %s AND action = 'update';
                """,
                (account_id,)
            )
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "user")
            self.assertEqual(row[1], self.user_id)
            self.assertIsNone(row[2])

    def test_audit_provenance_device_account_mutation(self):
        # 1. Create account via Device Bearer
        res = self.client.post(
            "/api/v1/accounts",
            headers={"Authorization": f"Bearer {self.raw_device_token}"},
            json={
                "name": "Device Cash Account",
                "account_type": "cash",
                "currency": "CNY"
            }
        )
        self.assertEqual(res.status_code, 201)
        account_id = UUID(res.json()["id"])

        # Verify audit_events for create
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT actor_type, actor_user_id, actor_device_id, action
                FROM audit_events
                WHERE entity_type = 'account' AND entity_id = %s AND action = 'create';
                """,
                (account_id,)
            )
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "device")
            self.assertEqual(row[1], self.user_id)
            self.assertEqual(row[2], self.device_id)

        # 2. Patch account via Device Bearer
        patch_res = self.client.patch(
            f"/api/v1/accounts/{account_id}",
            headers={"Authorization": f"Bearer {self.raw_device_token}"},
            json={
                "name": "Device Cash Account Renamed",
                "row_version": res.json()["row_version"]
            }
        )
        self.assertEqual(patch_res.status_code, 200)

        # Verify audit_events for update
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT actor_type, actor_user_id, actor_device_id, action
                FROM audit_events
                WHERE entity_type = 'account' AND entity_id = %s AND action = 'update';
                """,
                (account_id,)
            )
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "device")
            self.assertEqual(row[1], self.user_id)
            self.assertEqual(row[2], self.device_id)

    def test_audit_provenance_browser_category_mutation(self):
        # 1. Create category via Browser JWT
        res = self.client.post(
            "/api/v1/categories",
            headers={"Authorization": f"Bearer {self.browser_jwt_token}"},
            json={
                "name": "Browser Category",
                "type": "expense"
            }
        )
        self.assertEqual(res.status_code, 201)
        category_id = UUID(res.json()["id"])

        # Verify audit_events for create
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT actor_type, actor_user_id, actor_device_id, action
                FROM audit_events
                WHERE entity_type = 'category' AND entity_id = %s AND action = 'create';
                """,
                (category_id,)
            )
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "user")
            self.assertEqual(row[1], self.user_id)
            self.assertIsNone(row[2])

        # 2. Patch category via Browser JWT
        patch_res = self.client.patch(
            f"/api/v1/categories/{category_id}",
            headers={"Authorization": f"Bearer {self.browser_jwt_token}"},
            json={"name": "Browser Category Renamed"}
        )
        self.assertEqual(patch_res.status_code, 200)

        # Verify audit_events for update
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT actor_type, actor_user_id, actor_device_id, action
                FROM audit_events
                WHERE entity_type = 'category' AND entity_id = %s AND action = 'update';
                """,
                (category_id,)
            )
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "user")
            self.assertEqual(row[1], self.user_id)
            self.assertIsNone(row[2])

        # 3. Deactivate category via Browser JWT
        deact_res = self.client.post(
            f"/api/v1/categories/{category_id}/deactivate",
            headers={"Authorization": f"Bearer {self.browser_jwt_token}"}
        )
        self.assertEqual(deact_res.status_code, 200)

        # Verify audit_events for soft_delete
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT actor_type, actor_user_id, actor_device_id, action
                FROM audit_events
                WHERE entity_type = 'category' AND entity_id = %s AND action = 'soft_delete';
                """,
                (category_id,)
            )
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "user")
            self.assertEqual(row[1], self.user_id)
            self.assertIsNone(row[2])

    def test_audit_provenance_device_category_mutation(self):
        # 1. Create category via Device Bearer
        res = self.client.post(
            "/api/v1/categories",
            headers={"Authorization": f"Bearer {self.raw_device_token}"},
            json={
                "name": "Device Category",
                "type": "expense"
            }
        )
        self.assertEqual(res.status_code, 201)
        category_id = UUID(res.json()["id"])

        # Verify audit_events for create
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT actor_type, actor_user_id, actor_device_id, action
                FROM audit_events
                WHERE entity_type = 'category' AND entity_id = %s AND action = 'create';
                """,
                (category_id,)
            )
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "device")
            self.assertEqual(row[1], self.user_id)
            self.assertEqual(row[2], self.device_id)

        # 2. Patch category via Device Bearer
        patch_res = self.client.patch(
            f"/api/v1/categories/{category_id}",
            headers={"Authorization": f"Bearer {self.raw_device_token}"},
            json={"name": "Device Category Renamed"}
        )
        self.assertEqual(patch_res.status_code, 200)

        # Verify audit_events for update
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT actor_type, actor_user_id, actor_device_id, action
                FROM audit_events
                WHERE entity_type = 'category' AND entity_id = %s AND action = 'update';
                """,
                (category_id,)
            )
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "device")
            self.assertEqual(row[1], self.user_id)
            self.assertEqual(row[2], self.device_id)

        # 3. Deactivate category via Device Bearer
        deact_res = self.client.post(
            f"/api/v1/categories/{category_id}/deactivate",
            headers={"Authorization": f"Bearer {self.raw_device_token}"}
        )
        self.assertEqual(deact_res.status_code, 200)

        # Verify audit_events for soft_delete
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT actor_type, actor_user_id, actor_device_id, action
                FROM audit_events
                WHERE entity_type = 'category' AND entity_id = %s AND action = 'soft_delete';
                """,
                (category_id,)
            )
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "device")
            self.assertEqual(row[1], self.user_id)
            self.assertEqual(row[2], self.device_id)

    def test_audit_provenance_device_id_forwarding_statement(self):
        # Create bank account
        acc_id = uuid4()
        accounts_repo.create_account(
            self.conn,
            account_id=acc_id,
            household_id=self.household_id,
            name="Bank Statement Account",
            account_type="cash",
            currency="CNY",
            owner_user_id=self.user_id,
            status="active"
        )
        self.conn.commit()

        # Mock statement parser
        mock_result = StatementExtractionResult(
            account_number_mask="*1234",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            opening_balance=Decimal("1000.00"),
            closing_balance=Decimal("1000.00"),
            currency="CNY",
            lines=[]
        )
        parser = MockStatementParser(result=mock_result)
        statements_router._statement_parser = parser

        # Make dummy PDF bytes with text
        pdf_bytes = _make_pdf_bytes(["Bank Statement", "Account 1234", "Balance 1000 CNY"])

        # 1. Device statement upload
        res_dev = self.client.post(
            f"/api/v1/accounts/{acc_id}/statements",
            headers={"Authorization": f"Bearer {self.raw_device_token}"},
            files={"file": ("statement.pdf", pdf_bytes, "application/pdf")}
        )
        self.assertEqual(res_dev.status_code, 201)
        dev_batch_id = UUID(res_dev.json()["batch_id"])

        # Verify audit event for device upload
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT actor_type, actor_user_id, actor_device_id, action
                FROM audit_events
                WHERE entity_type = 'reconciliation_batch' AND entity_id = %s AND action = 'create';
                """,
                (dev_batch_id,)
            )
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "device")
            self.assertEqual(row[1], self.user_id)
            self.assertEqual(row[2], self.device_id)

        # 2. Browser statement upload
        res_browser = self.client.post(
            f"/api/v1/accounts/{acc_id}/statements",
            headers={"Authorization": f"Bearer {self.browser_jwt_token}"},
            files={"file": ("statement2.pdf", pdf_bytes, "application/pdf")}
        )
        self.assertEqual(res_browser.status_code, 201)
        browser_batch_id = UUID(res_browser.json()["batch_id"])

        # Verify audit event for browser upload
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT actor_type, actor_user_id, actor_device_id, action
                FROM audit_events
                WHERE entity_type = 'reconciliation_batch' AND entity_id = %s AND action = 'create';
                """,
                (browser_batch_id,)
            )
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "user")
            self.assertEqual(row[1], self.user_id)
            self.assertIsNone(row[2])


if __name__ == "__main__":
    unittest.main()
