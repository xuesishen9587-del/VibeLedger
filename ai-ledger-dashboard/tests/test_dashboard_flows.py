import os
import unittest
from unittest.mock import MagicMock, patch
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from time_utils import (
    format_iso_timestamp,
    get_dashboard_timezone,
    get_dashboard_today,
    get_dashboard_now
)
from dashboard_controller import (
    classify_candidates,
    format_candidate_options,
    is_ambiguous_match_candidate,
    is_category_required_candidate,
    build_category_patch_payload,
    is_batch_ready_to_commit
)
from api_client import ApiClient, AuthError, ConflictError, ValidationError


class TestDashboardFlows(unittest.TestCase):
    """
    Tests covering user-visible workflows and presentation logic for VibeLedger Dashboard.
    """

    def setUp(self):
        self.client = ApiClient(base_url="http://mock-backend:8000", auth_token="mock.token")

    def test_timezone_singapore_boundary(self):
        """
        Proves that when DASHBOARD_TIMEZONE is Asia/Singapore and UTC is 2026-08-27 23:30,
        the local business date in Singapore is 2026-08-28 (boundary check).
        """
        with patch.dict(os.environ, {"DASHBOARD_TIMEZONE": "Asia/Singapore"}):
            self.assertEqual(get_dashboard_timezone().key, "Asia/Singapore")

            # Mock datetime.now() with UTC 2026-08-27 23:30:00
            utc_dt = datetime(2026, 8, 27, 23, 30, 0, tzinfo=timezone.utc)
            with patch("time_utils.datetime") as mock_datetime:
                mock_datetime.now.side_effect = lambda tz=None: utc_dt.astimezone(tz or ZoneInfo("Asia/Singapore"))
                mock_datetime.combine = datetime.combine

                now_local = get_dashboard_now()
                self.assertEqual(now_local.year, 2026)
                self.assertEqual(now_local.month, 8)
                self.assertEqual(now_local.day, 28)
                self.assertEqual(now_local.hour, 7)
                self.assertEqual(now_local.minute, 30)

                today_local = get_dashboard_today()
                self.assertEqual(today_local, date(2026, 8, 28))

                iso_ts = format_iso_timestamp()
                self.assertTrue(iso_ts.startswith("2026-08-28T07:30:00+08:00"))

    def test_classify_candidates_canonical_lifecycle(self):
        """
        Proves that candidates with canonical statuses are correctly partitioned:
        - actionable: 'proposed', 'needs_review'
        - resolved: 'accepted', 'applied'
        - rejected: 'rejected'
        """
        candidates = [
            {"id": "c-1", "candidate_type": "match", "status": "needs_review"},
            {"id": "c-2", "candidate_type": "create_transaction", "status": "proposed"},
            {"id": "c-3", "candidate_type": "match", "status": "accepted"},
            {"id": "c-4", "candidate_type": "create_transaction", "status": "applied"},
            {"id": "c-5", "candidate_type": "match", "status": "rejected"}
        ]

        classified = classify_candidates(candidates)

        actionable_ids = [c["id"] for c in classified["actionable"]]
        resolved_ids = [c["id"] for c in classified["resolved"]]
        rejected_ids = [c["id"] for c in classified["rejected"]]

        self.assertEqual(actionable_ids, ["c-1", "c-2"])
        self.assertEqual(resolved_ids, ["c-3", "c-4"])
        self.assertEqual(rejected_ids, ["c-5"])

    def test_ambiguous_match_options_presentation_and_target_selection(self):
        """
        Proves that MULTIPLE_TRANSACTION_MATCHES candidates present options
        and submit the chosen target_transaction_id.
        """
        ambiguous_cand = {
            "id": "cand-amb-1",
            "candidate_type": "match",
            "status": "needs_review",
            "reason_code": "MULTIPLE_TRANSACTION_MATCHES",
            "options": [
                {
                    "transaction_id": "tx-opt-a",
                    "occurred_on": "2026-08-10",
                    "merchant": "Apple Store",
                    "amount": "999.00",
                    "currency": "CNY",
                    "match_score": 92
                },
                {
                    "transaction_id": "tx-opt-b",
                    "occurred_on": "2026-08-11",
                    "merchant": "Apple Online",
                    "amount": "999.00",
                    "currency": "CNY",
                    "match_score": 88
                }
            ]
        }

        self.assertTrue(is_ambiguous_match_candidate(ambiguous_cand))
        options = format_candidate_options(ambiguous_cand)
        self.assertEqual(len(options), 2)
        self.assertEqual(options[0]["transaction_id"], "tx-opt-a")
        self.assertEqual(options[1]["transaction_id"], "tx-opt-b")

        # Mock accepting with selected option B
        with patch.object(ApiClient, "request") as mock_request:
            mock_request.return_value = {"status": "accepted"}
            self.client.accept_reconciliation_candidate("cand-amb-1", target_transaction_id="tx-opt-b")
            mock_request.assert_called_once_with(
                "POST",
                "/api/v1/reconciliation-candidates/cand-amb-1/accept",
                json_data={"target_transaction_id": "tx-opt-b"}
            )

    def test_category_required_patch_flow(self):
        """
        Proves that CATEGORY_REQUIRED candidates trigger patch payload generation and execution.
        """
        cat_req_cand = {
            "id": "cand-cat-1",
            "candidate_type": "create_transaction",
            "status": "needs_review",
            "reason_code": "CATEGORY_REQUIRED",
            "payload": {
                "transaction": {
                    "transaction_type": "expense",
                    "occurred_on": "2026-08-15",
                    "amount": "250.00",
                    "currency": "CNY",
                    "category_id": None
                }
            }
        }

        self.assertTrue(is_category_required_candidate(cat_req_cand))

        chosen_cat_id = "cat-uuid-shopping"
        patch_payload = build_category_patch_payload(cat_req_cand, chosen_cat_id)
        self.assertEqual(patch_payload["transaction"]["category_id"], "cat-uuid-shopping")

        with patch.object(ApiClient, "request") as mock_request:
            mock_request.return_value = {"status": "needs_review", "payload": patch_payload}
            self.client.patch_reconciliation_candidate("cand-cat-1", patch_payload)
            mock_request.assert_called_once_with(
                "PATCH",
                "/api/v1/reconciliation-candidates/cand-cat-1",
                json_data={"payload": patch_payload}
            )

    def test_batch_readiness_and_concurrency(self):
        """
        Proves is_batch_ready_to_commit is False when actionable candidates remain,
        and becomes True when all candidates are resolved or rejected.
        """
        # 1. Unresolved batch
        preview_unresolved = {
            "batch": {"id": "batch-1", "row_version": 1, "status": "needs_review"},
            "candidates": [
                {"id": "c-1", "status": "needs_review"},
                {"id": "c-2", "status": "accepted"}
            ]
        }
        self.assertFalse(is_batch_ready_to_commit(preview_unresolved))

        # 2. Resolved batch
        preview_resolved = {
            "batch": {"id": "batch-1", "row_version": 2, "status": "needs_review"},
            "candidates": [
                {"id": "c-1", "status": "accepted"},
                {"id": "c-2", "status": "rejected"}
            ]
        }
        self.assertTrue(is_batch_ready_to_commit(preview_resolved))

        # 3. Commit with row_version
        with patch.object(ApiClient, "request") as mock_request:
            mock_request.return_value = {"status": "committed"}
            self.client.commit_reconciliation_batch("batch-1", row_version=2)
            mock_request.assert_called_once_with(
                "POST",
                "/api/v1/reconciliation-batches/batch-1/commit",
                json_data={"row_version": 2}
            )

    def test_snapshot_timestamp_helper_timezone_aware(self):
        """Proves format_iso_timestamp constructs timezone-aware ISO 8601 strings."""
        target_d = date(2026, 8, 27)
        iso_str = format_iso_timestamp(target_d)
        dt = datetime.fromisoformat(iso_str)
        self.assertIsNotNone(dt.tzinfo, "Constructed timestamp must be timezone-aware.")
        self.assertEqual(dt.date(), target_d)

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


if __name__ == "__main__":
    unittest.main()
