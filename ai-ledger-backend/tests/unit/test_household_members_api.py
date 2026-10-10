"""Member names are household-scoped browser reads, not a user directory."""
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4
from fastapi.testclient import TestClient
from app.main import create_app
from app.api.deps import get_auth_context, get_db_connection
from app.auth.context import AuthContext


class TestHouseholdMembersApi(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.conn = MagicMock()
        self.actor = AuthContext("browser", uuid4(), uuid4(), "member")
        self.app.dependency_overrides[get_db_connection] = lambda: self.conn
        self.client = TestClient(self.app)
        self.query = patch("app.api.routes.household_members.list_household_members").start()
        self.addCleanup(patch.stopall)
        self.query.return_value = [
            {"user_id": self.actor.user_id, "display_name": "First member", "user_status": "active",
             "email": "private@example.com", "role": "member", "household_id": self.actor.household_id},
            {"user_id": uuid4(), "display_name": "Second member", "user_status": "disabled"},
        ]

    def test_member_and_owner_can_read_only_current_household_names(self):
        for role in ("member", "owner"):
            with self.subTest(role=role):
                actor = AuthContext("browser", self.actor.user_id, self.actor.household_id, role)
                self.app.dependency_overrides[get_auth_context] = lambda: actor
                response = self.client.get("/api/v1/household-members", params={"household_id": str(uuid4())})
                self.assertEqual(response.status_code, 200, response.text)
                self.query.assert_called_with(self.conn, self.actor.household_id)
                data = response.json()
                self.assertIsNone(data["next_cursor"])
                self.assertEqual([m["display_name"] for m in data["items"]], ["First member", "Second member"])
                self.assertEqual([m["status"] for m in data["items"]], ["active", "disabled"])
                self.assertEqual(set(data["items"][0]), {"user_id", "display_name", "status"})
                self.assertNotIn("private@example.com", response.text)

    def test_device_cannot_read_member_directory(self):
        self.app.dependency_overrides[get_auth_context] = lambda: AuthContext(
            "device", self.actor.user_id, self.actor.household_id, "member", uuid4())
        self.assertEqual(self.client.get("/api/v1/household-members").status_code, 403)
        self.query.assert_not_called()

    def test_no_credentials_cannot_read_member_directory(self):
        self.assertEqual(self.client.get("/api/v1/household-members").status_code, 401)
        self.query.assert_not_called()

    def test_empty_family_does_not_invent_members(self):
        self.app.dependency_overrides[get_auth_context] = lambda: self.actor
        self.query.return_value = []
        self.assertEqual(self.client.get("/api/v1/household-members").json(), {"items": [], "next_cursor": None})
