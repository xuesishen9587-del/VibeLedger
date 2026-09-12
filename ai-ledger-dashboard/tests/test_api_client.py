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
            self.client.get_overview()
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
            self.client.get_transaction("non-existent-id")
        self.assertEqual(ctx.exception.status_code, 404)

    @patch.object(requests.Session, "request")
    def test_conflict_error_409(self, mock_request):
        mock_request.return_value = self._mock_response(
            409,
            {"error": {"code": "ROW_VERSION_CONFLICT", "message": "Modified concurrently", "retryable": True}}
        )
        with self.assertRaises(ConflictError) as ctx:
            self.client.commit_transaction_correction("tx-1", 1, {"merchant": "New"})
        self.assertEqual(ctx.exception.code, "ROW_VERSION_CONFLICT")
        self.assertTrue(ctx.exception.retryable)

    @patch.object(requests.Session, "request")
    def test_validation_error_422(self, mock_request):
        mock_request.return_value = self._mock_response(
            422,
            {"error": {"code": "VALIDATION_ERROR", "message": "Invalid amount"}}
        )
        with self.assertRaises(ValidationError) as ctx:
            self.client.create_account_snapshot("acc-1", Decimal("-10.00"))
        self.assertEqual(ctx.exception.status_code, 422)

    @patch.object(requests.Session, "request")
    def test_network_connection_error(self, mock_request):
        mock_request.side_effect = requests.exceptions.ConnectionError("Connection refused")
        with self.assertRaises(BackendUnavailableError):
            self.client.get_overview()

    @patch.object(requests.Session, "request")
    def test_network_timeout(self, mock_request):
        mock_request.side_effect = requests.exceptions.Timeout("Read timed out")
        with self.assertRaises(TimeoutError):
            self.client.get_overview()

    @patch.object(requests.Session, "request")
    def test_create_account_snapshot_serialization(self, mock_request):
        mock_request.return_value = self._mock_response(200, {"status": "committed"})
        self.client.create_account_snapshot(
            account_id="acc-123",
            balance=Decimal("1234.56"),
            as_of="2026-08-27T12:00:00+08:00",
            currency="CNY"
        )
        mock_request.assert_called_once()
        _, kwargs = mock_request.call_args
        self.assertEqual(kwargs["json"]["balance"], "1234.56")
        self.assertEqual(kwargs["json"]["as_of"], "2026-08-27T12:00:00+08:00")
        self.assertEqual(kwargs["json"]["currency"], "CNY")
        self.assertEqual(kwargs["json"]["source"], "dashboard_manual")
        self.assertNotIn("remarks", kwargs["json"])

    @patch.object(requests.Session, "request")
    def test_create_investment_snapshot_baseline(self, mock_request):
        mock_request.return_value = self._mock_response(201, {
            "status": "committed",
            "snapshot_id": "snap-baseline-1",
            "investment_pnl": None
        })
        res = self.client.create_investment_snapshot(
            account_id="acc-inv-1",
            total_asset_value=Decimal("50000.00"),
            currency="CNY",
            as_of="2026-08-27T14:00:00+08:00"
        )
        self.assertEqual(res["snapshot_id"], "snap-baseline-1")
        self.assertIsNone(res["investment_pnl"])
        mock_request.assert_called_once()
        _, kwargs = mock_request.call_args
        self.assertEqual(kwargs["json"]["total_asset_value"], "50000.00")
        self.assertEqual(kwargs["json"]["currency"], "CNY")
        self.assertEqual(kwargs["json"]["as_of"], "2026-08-27T14:00:00+08:00")
        self.assertEqual(kwargs["json"]["source"], "dashboard_manual")
        self.assertNotIn("closing_value", kwargs["json"])
        self.assertNotIn("contributions", kwargs["json"])

    @patch.object(requests.Session, "request")
    def test_create_investment_snapshot_with_pnl(self, mock_request):
        mock_request.return_value = self._mock_response(201, {
            "status": "committed",
            "snapshot_id": "snap-pnl-2",
            "investment_pnl": {
                "period_id": "period-1",
                "pnl_amount": "2500.00",
                "currency": "CNY",
                "status": "confirmed"
            }
        })
        res = self.client.create_investment_snapshot(
            account_id="acc-inv-1",
            total_asset_value=Decimal("52500.00"),
            currency="CNY",
            as_of="2026-08-27T15:00:00+08:00"
        )
        self.assertIsNotNone(res["investment_pnl"])
        self.assertEqual(res["investment_pnl"]["pnl_amount"], "2500.00")

    @patch.object(requests.Session, "request")
    def test_candidate_accept_reject_patch(self, mock_request):
        mock_request.return_value = self._mock_response(200, {"status": "accepted"})
        cand_id = "cand-uuid-1234"
        self.client.accept_reconciliation_candidate(cand_id, target_transaction_id="tx-uuid-5678")
        mock_request.assert_called_once()
        kwargs = mock_request.call_args.kwargs
        self.assertIn(f"/reconciliation-candidates/{cand_id}/accept", kwargs["url"])
        self.assertEqual(kwargs["json"]["target_transaction_id"], "tx-uuid-5678")

        mock_request.reset_mock()
        mock_request.return_value = self._mock_response(200, {"status": "rejected"})
        self.client.reject_reconciliation_candidate(cand_id, reason="User ignore")
        kwargs = mock_request.call_args.kwargs
        self.assertIn(f"/reconciliation-candidates/{cand_id}/reject", kwargs["url"])
        self.assertEqual(kwargs["json"]["reason"], "User ignore")

    @patch.object(requests.Session, "request")
    def test_upload_statement_multipart(self, mock_request):
        mock_request.return_value = self._mock_response(200, {"batch_id": "batch-123"})
        res = self.client.upload_statement(
            account_id="acc-123",
            file_bytes=b"%PDF-1.4 mock content",
            filename="stmt.pdf",
            password="pass"
        )
        self.assertEqual(res["batch_id"], "batch-123")
        mock_request.assert_called_once()
        _, kwargs = mock_request.call_args
        self.assertIn("file", kwargs["files"])
        self.assertEqual(kwargs["data"]["password"], "pass")

    @patch.object(requests.Session, "request")
    def test_reconciliation_batch_commit(self, mock_request):
        mock_request.return_value = self._mock_response(200, {"status": "committed"})
        self.client.commit_reconciliation_batch("batch-1", row_version=2)
        mock_request.assert_called_once()
        _, kwargs = mock_request.call_args
        self.assertEqual(kwargs["json"]["row_version"], 2)

    @patch.object(requests.Session, "request")
    def test_work_queue_query(self, mock_request):
        mock_request.return_value = self._mock_response(200, {"items": [{"id": "w-1", "work_type": "ingestion"}]})
        res = self.client.get_work_queue(type_filter="ingestion")
        self.assertEqual(len(res["items"]), 1)
        mock_request.assert_called_once()
        _, kwargs = mock_request.call_args
        self.assertEqual(kwargs["params"]["type"], "ingestion")

    @patch.object(requests.Session, "request")
    def test_audit_events_query(self, mock_request):
        mock_request.return_value = self._mock_response(200, {"items": [], "next_cursor": None})
        self.client.list_audit_events(entity_type="transaction", limit=20)
        mock_request.assert_called_once()
        _, kwargs = mock_request.call_args
        self.assertEqual(kwargs["params"]["entity_type"], "transaction")
        self.assertEqual(kwargs["params"]["limit"], 20)

    @patch.object(requests.Session, "request")
    def test_void_transaction(self, mock_request):
        mock_request.return_value = self._mock_response(200, {"status": "voided", "account_balance_restored": True})
        res = self.client.void_transaction("tx-1", delete_reason="Duplicate", expected_version=0)
        self.assertEqual(res["status"], "voided")
        mock_request.assert_called_once()
        _, kwargs = mock_request.call_args
        self.assertEqual(kwargs["json"]["delete_reason"], "Duplicate")
        self.assertEqual(kwargs["json"]["expected_version"], 0)

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
