import unittest
from unittest.mock import patch, MagicMock
from decimal import Decimal
import requests

from api_client import (
    ApiClient,
    ApiError,
    AuthError,
    ForbiddenError,
    NotFoundError,
    ConflictError,
    ValidationError,
    ServiceUnavailableError,
    BackendUnavailableError,
    TimeoutError
)


class TestApiClient(unittest.TestCase):
    def setUp(self):
        self.client = ApiClient(base_url="http://test-backend:8000", auth_token="test.jwt.token")

    def _mock_response(self, status_code=200, json_data=None, text="", content=b""):
        resp = MagicMock()
        resp.status_code = status_code
        resp.content = content or (b"{}" if json_data is not None else b"")
        resp.json.return_value = json_data if json_data is not None else {}
        resp.text = text or (str(json_data) if json_data is not None else "")
        return resp

    @patch.object(requests.Session, "request")
    def test_request_headers_and_auth_token(self, mock_request):
        mock_request.return_value = self._mock_response(200, {"status": "ok"})
        res = self.client.health_check()
        self.assertEqual(res, {"status": "ok"})
        mock_request.assert_called_once()
        _, kwargs = mock_request.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test.jwt.token")
        self.assertEqual(kwargs["headers"]["Accept"], "application/json")

    @patch.object(requests.Session, "request")
    def test_auth_error_401(self, mock_request):
        mock_request.return_value = self._mock_response(
            401,
            {"error": {"code": "UNAUTHORIZED", "message": "Invalid token", "retryable": False}}
        )
        with self.assertRaises(AuthError) as ctx:
            self.client.request("GET", "/api/v1/reports/wealth")
        self.assertEqual(ctx.exception.code, "UNAUTHORIZED")
        self.assertEqual(ctx.exception.status_code, 401)

    @patch.object(requests.Session, "request")
    def test_forbidden_error_403(self, mock_request):
        mock_request.return_value = self._mock_response(
            403,
            {"error": {"code": "FORBIDDEN", "message": "Access denied"}}
        )
        with self.assertRaises(ForbiddenError) as ctx:
            self.client.list_accounts()
        self.assertEqual(ctx.exception.status_code, 403)

    @patch.object(requests.Session, "request")
    def test_not_found_error_404(self, mock_request):
        mock_request.return_value = self._mock_response(
            404,
            {"error": {"code": "NOT_FOUND", "message": "Transaction not found"}}
        )
        with self.assertRaises(NotFoundError) as ctx:
            self.client.request("GET", "/api/v1/transactions/non-existent-id")
        self.assertEqual(ctx.exception.status_code, 404)

    @patch.object(requests.Session, "request")
    def test_conflict_error_409(self, mock_request):
        mock_request.return_value = self._mock_response(
            409,
            {"error": {"code": "ROW_VERSION_CONFLICT", "message": "Modified concurrently", "retryable": True}}
        )
        with self.assertRaises(ConflictError) as ctx:
            self.client.request("PATCH", "/api/v1/transactions/tx-1", json_data={"expected_version": 1, "merchant": "New"})
        self.assertEqual(ctx.exception.code, "ROW_VERSION_CONFLICT")
        self.assertTrue(ctx.exception.retryable)

    @patch.object(requests.Session, "request")
    def test_validation_error_422(self, mock_request):
        mock_request.return_value = self._mock_response(
            422,
            {"error": {"code": "VALIDATION_ERROR", "message": "Invalid amount"}}
        )
        with self.assertRaises(ValidationError) as ctx:
            self.client.request("POST", "/api/v1/balance-observations", json_data={"balance": "invalid"})
        self.assertEqual(ctx.exception.status_code, 422)

    @patch.object(requests.Session, "request")
    def test_network_connection_error(self, mock_request):
        mock_request.side_effect = requests.exceptions.ConnectionError("Connection refused")
        with self.assertRaises(BackendUnavailableError):
            self.client.request("GET", "/api/v1/reports/wealth")

    @patch.object(requests.Session, "request")
    def test_network_timeout(self, mock_request):
        mock_request.side_effect = requests.exceptions.Timeout("Read timed out")
        with self.assertRaises(TimeoutError):
            self.client.request("GET", "/api/v1/reports/wealth")










    @patch.object(requests.Session, "request")
    def test_account_and_category_management(self, mock_request):
        mock_request.return_value = self._mock_response(200, {"status": "ok"})
        self.client.create_account(
            name="Checking",
            balance_scope="main cash",
            account_type="savings",
            currency="CNY",
            idempotency_key="key-acc-create",
            risk_level="low",
            opened_on="2026-01-01",
            statement_import_enabled=False
        )
        self.client.update_account("acc-1", {"name": "New Name", "expected_version": 1}, idempotency_key="key-acc-patch")
        self.client.create_account_alias("acc-1", "Alias 1", idempotency_key="key-alias-create")
        self.client.update_account_alias("acc-1", "al-1", {"status": "inactive", "expected_version": 0}, idempotency_key="key-alias-patch")
        self.client.create_category("Dining Out", "expense", idempotency_key="key-cat-create", description="Food")
        self.client.update_category("cat-1", {"name": "Dining", "expected_version": 2}, idempotency_key="key-cat-patch")
        self.assertEqual(mock_request.call_count, 6)

        # Verify obsolete methods do not exist on client
        self.assertFalse(hasattr(self.client, "deactivate_account"))
        self.assertFalse(hasattr(self.client, "deactivate_category"))
        self.assertFalse(hasattr(self.client, "delete_account_alias"))

    @patch.object(requests.Session, "request")
    def test_account_create_canonical_contract(self, mock_request):
        mock_request.return_value = self._mock_response(201, {"id": "acc-new", "name": "Salary Account"})
        res = self.client.create_account(
            name="Salary Account",
            balance_scope="main cash",
            account_type="cash",
            currency="CNY",
            idempotency_key="idemp-acc-001",
            owner_user_id="user-uuid-1",
            risk_level="very_low",
            opened_on="2026-01-15",
            statement_import_enabled=True,
        )
        self.assertEqual(res["id"], "acc-new")
        mock_request.assert_called_once()
        method, url = mock_request.call_args[0] if len(mock_request.call_args[0]) >= 2 else (mock_request.call_args.kwargs.get("method"), mock_request.call_args.kwargs.get("url"))
        kwargs = mock_request.call_args[1]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/api/v1/accounts"))
        self.assertEqual(kwargs["headers"]["Idempotency-Key"], "idemp-acc-001")
        self.assertEqual(kwargs["json"], {
            "name": "Salary Account",
            "balance_scope": "main cash",
            "account_type": "cash",
            "currency": "CNY",
            "owner_user_id": "user-uuid-1",
            "risk_level": "very_low",
            "opened_on": "2026-01-15",
            "statement_import_enabled": True,
        })
        # Assert zero forbidden residual fields in payload
        for forbidden in ["institution", "billing_day", "due_day", "linked_cash_account_id"]:
            self.assertNotIn(forbidden, kwargs["json"])

    @patch.object(requests.Session, "request")
    def test_account_patch_canonical_contract(self, mock_request):
        mock_request.return_value = self._mock_response(200, {"id": "acc-1", "name": "Updated Name"})
        patch_payload = {
            "expected_version": 3,
            "name": "Updated Name",
            "balance_scope": "updated scope",
            "risk_level": "medium",
            "opened_on": "2026-02-01",
            "statement_import_enabled": False,
        }
        res = self.client.update_account("acc-1", patch_payload, idempotency_key="idemp-patch-acc-001")
        self.assertEqual(res["name"], "Updated Name")
        mock_request.assert_called_once()
        kwargs = mock_request.call_args[1]
        self.assertEqual(kwargs["headers"]["Idempotency-Key"], "idemp-patch-acc-001")
        self.assertEqual(kwargs["json"], patch_payload)

    @patch.object(requests.Session, "request")
    def test_alias_create_and_patch_contract(self, mock_request):
        mock_request.return_value = self._mock_response(201, {"id": "al-new", "alias": "Card Alias"})
        self.client.create_account_alias("acc-1", "Card Alias", idempotency_key="idemp-al-create")
        _, kwargs_create = mock_request.call_args
        self.assertEqual(kwargs_create["headers"]["Idempotency-Key"], "idemp-al-create")
        self.assertEqual(kwargs_create["json"], {"alias": "Card Alias"})

        mock_request.return_value = self._mock_response(200, {"id": "al-1", "status": "inactive"})
        patch_payload = {"expected_version": 2, "status": "inactive"}
        self.client.update_account_alias("acc-1", "al-1", patch_payload, idempotency_key="idemp-al-patch")
        _, kwargs_patch = mock_request.call_args
        self.assertEqual(kwargs_patch["headers"]["Idempotency-Key"], "idemp-al-patch")
        self.assertEqual(kwargs_patch["json"], patch_payload)

    @patch.object(requests.Session, "request")
    def test_category_create_and_patch_contract(self, mock_request):
        mock_request.return_value = self._mock_response(201, {"id": "cat-new", "name": "Electronics"})
        self.client.create_category("Electronics", "expense", idempotency_key="idemp-cat-create", description="Gadgets")
        _, kwargs_create = mock_request.call_args
        self.assertEqual(kwargs_create["headers"]["Idempotency-Key"], "idemp-cat-create")
        self.assertEqual(kwargs_create["json"], {"name": "Electronics", "type": "expense", "description": "Gadgets"})

        mock_request.return_value = self._mock_response(200, {"id": "cat-1", "status": "inactive"})
        patch_payload = {"expected_version": 1, "status": "inactive", "description": "Archived"}
        self.client.update_category("cat-1", patch_payload, idempotency_key="idemp-cat-patch")
        _, kwargs_patch = mock_request.call_args
        self.assertEqual(kwargs_patch["headers"]["Idempotency-Key"], "idemp-cat-patch")
        self.assertEqual(kwargs_patch["json"], patch_payload)
