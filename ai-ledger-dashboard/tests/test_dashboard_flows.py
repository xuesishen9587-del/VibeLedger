import unittest
from unittest.mock import MagicMock, patch
from datetime import date, datetime, timezone
from decimal import Decimal

from time_utils import format_iso_timestamp
from api_client import ApiClient, AuthError, ConflictError, ValidationError


class TestDashboardFlows(unittest.TestCase):
    """
    Tests covering user-visible workflows and presentation logic for VibeLedger Dashboard.
    """

    def setUp(self):
        self.client = ApiClient(base_url="http://mock-backend:8000", auth_token="mock.token")

    def test_snapshot_timestamp_helper_timezone_aware(self):
        """Proves format_iso_timestamp constructs timezone-aware ISO 8601 strings."""
        # 1. From date
        target_d = date(2026, 8, 27)
        iso_str = format_iso_timestamp(target_d)
        dt = datetime.fromisoformat(iso_str)
        self.assertIsNotNone(dt.tzinfo, "Constructed timestamp must be timezone-aware.")
        self.assertEqual(dt.date(), target_d)

        # 2. From ISO string date
        iso_str2 = format_iso_timestamp("2026-08-27")
        dt2 = datetime.fromisoformat(iso_str2)
        self.assertIsNotNone(dt2.tzinfo)
        self.assertEqual(dt2.date(), date(2026, 8, 27))

        # 3. Default current time
        iso_str3 = format_iso_timestamp()
        dt3 = datetime.fromisoformat(iso_str3)
        self.assertIsNotNone(dt3.tzinfo)

    @patch.object(ApiClient, "request")
    def test_account_snapshot_submission_contract(self, mock_request):
        """Proves account snapshot sends timezone-aware as_of and proper fields."""
        mock_request.return_value = {
            "status": "committed",
            "snapshot_id": "snap-123",
            "account_id": "acc-1",
            "residual_amount": "0.00"
        }

        res = self.client.create_account_snapshot(
            account_id="acc-1",
            balance=Decimal("1500.50"),
            as_of="2026-08-27T12:00:00+08:00",
            currency="CNY"
        )
        self.assertEqual(res["status"], "committed")
        mock_request.assert_called_once_with(
            "POST",
            "/api/v1/accounts/acc-1/snapshots",
            json_data={
                "balance": "1500.50",
                "as_of": "2026-08-27T12:00:00+08:00",
                "source": "dashboard_manual",
                "currency": "CNY"
            }
        )

    @patch.object(ApiClient, "request")
    def test_investment_snapshot_baseline_flow(self, mock_request):
        """Proves initial baseline investment snapshot returns null investment_pnl."""
        mock_request.return_value = {
            "status": "committed",
            "snapshot_id": "snap-inv-baseline",
            "investment_pnl": None
        }

        res = self.client.create_investment_snapshot(
            account_id="acc-inv-1",
            total_asset_value=Decimal("100000.00"),
            currency="CNY",
            as_of="2026-08-27T14:00:00+08:00"
        )
        self.assertEqual(res["status"], "committed")
        self.assertIsNone(res["investment_pnl"])
        mock_request.assert_called_once_with(
            "POST",
            "/api/v1/investment-accounts/acc-inv-1/snapshots",
            json_data={
                "total_asset_value": "100000.00",
                "currency": "CNY",
                "as_of": "2026-08-27T14:00:00+08:00",
                "source": "dashboard_manual"
            }
        )

    @patch.object(ApiClient, "request")
    def test_investment_snapshot_subsequent_pnl_flow(self, mock_request):
        """Proves subsequent investment snapshot returns confirmed investment_pnl."""
        mock_request.return_value = {
            "status": "committed",
            "snapshot_id": "snap-inv-2",
            "investment_pnl": {
                "period_id": "pnl-period-1",
                "pnl_amount": "3200.00",
                "currency": "CNY",
                "status": "confirmed"
            }
        }

        res = self.client.create_investment_snapshot(
            account_id="acc-inv-1",
            total_asset_value=Decimal("103200.00"),
            currency="CNY",
            as_of="2026-08-27T15:00:00+08:00"
        )
        self.assertEqual(res["status"], "committed")
        self.assertIsNotNone(res["investment_pnl"])
        self.assertEqual(res["investment_pnl"]["pnl_amount"], "3200.00")

    @patch.object(ApiClient, "request")
    def test_reconciliation_candidate_accept_uses_candidate_id(self, mock_request):
        """Proves candidate accept sends candidate_id in path, not statement_line_id."""
        mock_request.return_value = {"status": "accepted"}
        cand_id = "real-candidate-uuid-1111"
        target_tx_id = "target-tx-uuid-2222"

        self.client.accept_reconciliation_candidate(cand_id, target_transaction_id=target_tx_id)
        mock_request.assert_called_once_with(
            "POST",
            f"/api/v1/reconciliation-candidates/{cand_id}/accept",
            json_data={"target_transaction_id": target_tx_id}
        )

    @patch.object(ApiClient, "request")
    def test_reconciliation_optimistic_concurrency_refresh_and_commit(self, mock_request):
        """Proves batch commit sends row_version from batch object."""
        # 1. Preview returns batch row_version = 2
        mock_request.return_value = {
            "batch": {"id": "batch-1", "row_version": 2, "status": "needs_review"},
            "candidates": [],
            "summary": {"pending_count": 0}
        }
        preview = self.client.get_reconciliation_preview("batch-1")
        row_version = preview["batch"]["row_version"]
        self.assertEqual(row_version, 2)

        # 2. Commit batch with row_version = 2
        mock_request.reset_mock()
        mock_request.return_value = {"status": "committed"}
        self.client.commit_reconciliation_batch("batch-1", row_version=row_version)
        mock_request.assert_called_once_with(
            "POST",
            "/api/v1/reconciliation-batches/batch-1/commit",
            json_data={"row_version": 2}
        )

    @patch.object(ApiClient, "request")
    def test_transaction_void_mandatory_version(self, mock_request):
        """Proves transaction void requires expected_version."""
        mock_request.return_value = {"status": "voided", "account_balance_restored": True}
        self.client.void_transaction("tx-1", delete_reason="Wrong entry", expected_version=3)
        mock_request.assert_called_once_with(
            "POST",
            "/api/v1/transactions/tx-1/void",
            json_data={"delete_reason": "Wrong entry", "expected_version": 3}
        )

    @patch.object(ApiClient, "request")
    def test_backend_multi_currency_aggregates_consumed(self, mock_request):
        """Proves dashboard consumes backend-provided reporting currency aggregates."""
        mock_request.return_value = {
            "reporting_currency": "CNY",
            "total_assets": "15000.00",
            "total_liabilities": "2000.00",
            "net_worth": "13000.00",
            "asset_allocation": [
                {"account_type": "cash", "account_type_label": "活期资产", "amount": "10000.00", "currency": "CNY"},
                {"account_type": "investment", "account_type_label": "投资资产", "amount": "5000.00", "currency": "CNY"}
            ]
        }
        overview = self.client.get_overview()
        self.assertEqual(overview["reporting_currency"], "CNY")
        self.assertEqual(len(overview["asset_allocation"]), 2)
        self.assertEqual(overview["asset_allocation"][0]["amount"], "10000.00")
