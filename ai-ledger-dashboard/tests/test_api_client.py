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
            as_of="2026-08-27",
            currency="CNY",
            remarks="Unit test snapshot"
        )
        mock_request.assert_called_once()
        _, kwargs = mock_request.call_args
        self.assertEqual(kwargs["json"]["balance"], "1234.56")
        self.assertEqual(kwargs["json"]["as_of"], "2026-08-27")
        self.assertEqual(kwargs["json"]["currency"], "CNY")

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
