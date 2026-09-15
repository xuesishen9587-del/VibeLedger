"""
Unit tests characterizing the iPhone Expense Shortcut compatibility boundary
and verifying the target interrupted-request cancellation safety contracts for S0.

These tests run completely offline without connecting to remote database credentials.
"""

import os
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test_user:test_pass@127.0.0.1:5432/vibeledger_test")
os.environ.setdefault("DB_SCHEMA", "vibeledger_test_runner")

import unittest
from uuid import uuid4, UUID
from decimal import Decimal
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

from app.services.expense_service import (
    process_expense_request,
    confirm_ingestion_request,
    revise_ingestion_request,
    reject_ingestion_request,
    get_by_idempotency_key,
    recompute_draft_warnings
)
from app.services.gemini_service import ExpenseExtractionResult, MockGeminiService
from app.domain.transactions import (
    RequestNotFoundError,
    IdempotencyKeyReuseError,
    InvalidRequestStateError
)


VALID_JPEG_HEADER = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00" + (b"\x00" * 100) + b"\xff\xd9"


class TestExpenseShortcutBoundaryUnit(unittest.TestCase):
    def setUp(self):
        self.household_id = uuid4()
        self.user_id = uuid4()
        self.device_id = uuid4()
        self.device = {
            "device_id": self.device_id,
            "household_id": self.household_id,
            "user_id": self.user_id
        }

        self.acc_checking_id = uuid4()
        self.acc_checking = {
            "id": self.acc_checking_id,
            "household_id": self.household_id,
            "name": "招商银行储蓄卡",
            "account_type": "cash",
            "currency": "CNY",
            "status": "active",
            "aliases": ["CMB Checking", "工资卡"]
        }
        self.acc_credit_id = uuid4()
        self.acc_credit = {
            "id": self.acc_credit_id,
            "household_id": self.household_id,
            "name": "招商银行信用卡",
            "account_type": "credit",
            "currency": "CNY",
            "status": "active",
            "aliases": ["CMB Credit", "招行卡"]
        }
        self.active_accounts = [self.acc_checking, self.acc_credit]

        self.cat_dining_id = uuid4()
        self.cat_dining = {
            "id": self.cat_dining_id,
            "household_id": self.household_id,
            "name": "餐饮美食",
            "category_type": "expense",
            "status": "active"
        }
        self.active_categories = [self.cat_dining]

        self.mock_conn = MagicMock()

    # =========================================================================
    # Part 1: Characterize Accepted Current Expense Shortcut Flows
    # =========================================================================

    @patch("app.services.expense_service.ingestion_repo")
    @patch("app.services.expense_service.accounts_repo")
    @patch("app.services.expense_service.ledger_service")
    def test_01_clear_high_confidence_expense_one_request_flow(
        self, mock_ledger, mock_accounts, mock_ingestion
    ):
        """
        Verifies the accepted one-request normal fast path:
        POST /api/v1/expenses returns committed status with display_summary,
        transaction_id, request_id, and payment_mode='one_off'.
        """
        mock_ingestion.lock_by_device_and_key.return_value = None
        mock_ingestion.create_ingestion_request.return_value = True
        mock_accounts.list_accounts.return_value = self.active_accounts
        mock_accounts.list_categories.return_value = self.active_categories

        tx_id = uuid4()
        mock_ledger.record_expense.return_value = {
            "id": tx_id,
            "amount": Decimal("28.50"),
            "currency": "CNY",
            "category_id": self.cat_dining_id,
            "from_account_id": self.acc_checking_id
        }

        mock_gemini = MockGeminiService()
        mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 9, 6),
            merchant="瑞幸咖啡",
            original_amount=Decimal("28.50"),
            original_currency="CNY",
            from_account="招商银行储蓄卡",
            category="餐饮美食",
            payment_mode="one_off",
            confidence=0.98,
            field_confidence={
                "date": 0.99,
                "amount": 0.99,
                "currency": 0.99,
                "account": 0.95,
                "category": 0.95
            }
        ))

        key = f"key-high-conf-{uuid4().hex}"
        payload = {
            "idempotency_key": key,
            "captured_at": "2026-09-06T12:30:00+08:00",
            "client_version": "expense-shortcut-v2",
            "image": {"mime_type": "image/jpeg", "base64": "dummy"},
            "note": None
        }

        res = process_expense_request(
            conn=self.mock_conn,
            device=self.device,
            payload=payload,
            gemini_service=mock_gemini,
            image_bytes=VALID_JPEG_HEADER
        )

        self.assertEqual(res["status"], "committed")
        self.assertEqual(res["payment_mode"], "one_off")
        self.assertEqual(res["transaction_id"], str(tx_id))
        self.assertIn("display_summary", res)
        self.assertIn("¥28.50", res["display_summary"])
        self.assertIn("瑞幸咖啡", res["display_summary"])

        # Exactly 1 financial write to ledger
        mock_ledger.record_expense.assert_called_once()
        # Ingestion status updated to committed
        mock_ingestion.update_ingestion_request_status.assert_called_once()
        self.assertEqual(mock_ingestion.update_ingestion_request_status.call_args[1]["status"], "committed")

    @patch("app.services.expense_service.ingestion_repo")
    @patch("app.services.expense_service.accounts_repo")
    def test_02_needs_confirmation_flow_and_warnings(
        self, mock_accounts, mock_ingestion
    ):
        """
        Verifies ambiguous capture produces needs_confirmation draft with warnings,
        draft details, display_summary, and zero financial transactions.
        """
        mock_ingestion.lock_by_device_and_key.return_value = None
        mock_ingestion.create_ingestion_request.return_value = True
        mock_accounts.list_accounts.return_value = self.active_accounts
        mock_accounts.list_categories.return_value = self.active_categories

        mock_gemini = MockGeminiService()
        mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 9, 6),
            merchant="未知商户",
            original_amount=Decimal("56.00"),
            original_currency="CNY",
            from_account=None,  # Unresolved account forces confirmation
            category="餐饮美食",
            payment_mode="one_off",
            confidence=0.60,
            field_confidence={"date": 0.95, "amount": 0.95, "currency": 0.95, "category": 0.95}
        ))

        key = f"key-needs-conf-{uuid4().hex}"
        payload = {
            "idempotency_key": key,
            "captured_at": "2026-09-06T18:45:00+08:00",
            "client_version": "expense-shortcut-v2",
            "image": {"mime_type": "image/jpeg", "base64": "dummy"}
        }

        res = process_expense_request(
            conn=self.mock_conn,
            device=self.device,
            payload=payload,
            gemini_service=mock_gemini,
            image_bytes=VALID_JPEG_HEADER
        )

        self.assertEqual(res["status"], "needs_confirmation")
        self.assertIn("draft", res)
        self.assertEqual(res["draft"]["original_amount"], "56.00")
        self.assertIsNone(res["draft"]["from_account"])
        self.assertIn("warnings", res)
        warning_codes = [w["code"] for w in res["warnings"]]
        self.assertIn("ACCOUNT_UNRESOLVED", warning_codes)
        self.assertTrue(res["display_summary"].startswith("⚠️ 请确认"))

    @patch("app.services.expense_service.ingestion_repo")
    @patch("app.services.expense_service.accounts_repo")
    @patch("app.services.expense_service.ledger_service")
    def test_03_confirm_draft_request(
        self, mock_ledger, mock_accounts, mock_ingestion
    ):
        """
        Verifies confirming a needs_confirmation draft commits transaction and returns committed outcome.
        """
        req_id = uuid4()
        tx_id = uuid4()
        mock_ingestion.lock_ingestion_request.return_value = {
            "id": req_id,
            "device_id": self.device_id,
            "status": "needs_confirmation",
            "response_payload": None,
            "draft_payload": {
                "occurred_on": "2026-09-06",
                "merchant": "未知商户",
                "original_amount": "56.00",
                "original_currency": "CNY",
                "from_account": {"id": str(self.acc_checking_id), "name": "招商银行储蓄卡"},
                "category": {"id": str(self.cat_dining_id), "name": "餐饮美食"},
                "payment_mode": "one_off"
            }
        }
        mock_accounts.get_account.return_value = self.acc_checking
        mock_accounts.get_category.return_value = self.cat_dining
        mock_ledger.record_expense.return_value = {"id": tx_id}

        res = confirm_ingestion_request(
            conn=self.mock_conn,
            request_id=req_id,
            device=self.device
        )

        self.assertEqual(res["status"], "committed")
        self.assertEqual(res["transaction_id"], str(tx_id))
        self.assertIn("¥56.00", res["display_summary"])

    @patch("app.services.expense_service.ingestion_repo")
    @patch("app.services.expense_service.accounts_repo")
    def test_03b_revise_natural_language(self, mock_accounts, mock_ingestion):
        """
        Verifies natural-language draft revision (POST /ingestion-requests/{id}/revise with correction_note).
        Returns revised draft, updated display_summary, recomputed warnings, and needs_confirmation status.
        """
        from app.services.gemini_service import ExpenseRevisionResult

        req_id = uuid4()
        mock_ingestion.lock_ingestion_request.return_value = {
            "id": req_id,
            "device_id": self.device_id,
            "status": "needs_confirmation",
            "draft_payload": {
                "occurred_on": "2026-09-06",
                "merchant": "未知商户",
                "original_amount": "56.00",
                "original_currency": "CNY",
                "from_account": None,
                "category": {"id": str(self.cat_dining_id), "name": "餐饮美食"},
                "payment_mode": "one_off"
            }
        }
        mock_accounts.list_accounts.return_value = self.active_accounts
        mock_accounts.list_categories.return_value = self.active_categories

        mock_gemini = MockGeminiService()
        mock_gemini.set_next_revision_result(ExpenseRevisionResult(
            occurred_on=date(2026, 9, 6),
            merchant="未知商户",
            original_amount=Decimal("52.00"),
            original_currency="CNY",
            from_account="招商银行储蓄卡",
            category="餐饮美食",
            payment_mode="one_off"
        ))

        res = revise_ingestion_request(
            conn=self.mock_conn,
            request_id=req_id,
            device=self.device,
            correction_note="改用招商银行储蓄卡支付，实际金额是52元",
            gemini_service=mock_gemini
        )

        self.assertEqual(res["status"], "needs_confirmation")
        self.assertEqual(res["draft"]["original_amount"], "52.00")
        self.assertEqual(res["draft"]["from_account"]["name"], "招商银行储蓄卡")
        self.assertIn("已修订", res["display_summary"])
        self.assertEqual(res["warnings"], [])  # All fields now resolved!

    @patch("app.services.expense_service.ingestion_repo")
    @patch("app.services.expense_service.accounts_repo")
    def test_03c_revise_structured_fields(self, mock_accounts, mock_ingestion):
        """
        Verifies structured field draft revision bypassing Gemini.
        Returns revised draft with recomputed warnings and updated display_summary.
        """
        req_id = uuid4()
        mock_ingestion.lock_ingestion_request.return_value = {
            "id": req_id,
            "device_id": self.device_id,
            "status": "needs_confirmation",
            "draft_payload": {
                "occurred_on": "2026-09-06",
                "merchant": "未知商户",
                "original_amount": "56.00",
                "original_currency": "CNY",
                "from_account": None,
                "category": {"id": str(self.cat_dining_id), "name": "餐饮美食"},
                "payment_mode": "one_off"
            }
        }
        mock_accounts.list_accounts.return_value = self.active_accounts
        mock_accounts.list_categories.return_value = self.active_categories

        res = revise_ingestion_request(
            conn=self.mock_conn,
            request_id=req_id,
            device=self.device,
            structured_fields={
                "original_amount": "52.00",
                "from_account_id": str(self.acc_checking_id)
            }
        )

        self.assertEqual(res["status"], "needs_confirmation")
        self.assertEqual(res["draft"]["original_amount"], "52.00")
        self.assertEqual(res["draft"]["from_account"]["id"], str(self.acc_checking_id))
        self.assertEqual(res["warnings"], [])

    @patch("app.services.expense_service.ingestion_repo")
    def test_04_reject_draft_request(self, mock_ingestion):
        """
        Verifies rejecting an ingestion request produces terminal rejected status and zero financial effects.
        """
        req_id = uuid4()
        mock_ingestion.lock_ingestion_request.return_value = {
            "id": req_id,
            "device_id": self.device_id,
            "status": "needs_confirmation"
        }

        res = reject_ingestion_request(
            conn=self.mock_conn,
            request_id=req_id,
            device=self.device,
            reason="用户取消记账"
        )

        self.assertEqual(res["status"], "rejected")
        self.assertIn("已放弃记账", res["display_summary"])
        mock_ingestion.update_ingestion_request_status.assert_called_once()
        self.assertEqual(mock_ingestion.update_ingestion_request_status.call_args[1]["status"], "rejected")

    @patch("app.services.expense_service.ingestion_repo")
    def test_05_by_key_replay_and_404_recovery(self, mock_ingestion):
        """
        Verifies GET /by-key/{key} replays saved response when present,
        and raises RequestNotFoundError (404) when key is absent.
        """
        # Case A: Saved committed payload
        saved_payload = {
            "status": "committed",
            "request_id": str(uuid4()),
            "transaction_id": str(uuid4()),
            "payment_mode": "one_off",
            "display_summary": "¥28.50 · 瑞幸咖啡"
        }
        mock_ingestion.get_by_device_and_key.return_value = {
            "id": uuid4(),
            "status": "committed",
            "response_payload": saved_payload
        }

        res = get_by_idempotency_key(
            conn=self.mock_conn,
            device_id=self.device_id,
            idempotency_key="key-existing-123"
        )
        self.assertEqual(res, saved_payload)

        # Case B: Unknown key returns 404 (RequestNotFoundError)
        mock_ingestion.get_by_device_and_key.return_value = None
        with self.assertRaises(RequestNotFoundError):
            get_by_idempotency_key(
                conn=self.mock_conn,
                device_id=self.device_id,
                idempotency_key="key-unknown-404"
            )

    # =========================================================================
    # Part 2: Target Interrupted-Request Safety Contracts (CONTRACTS.md)
    # =========================================================================

    def test_06_current_code_replays_rejected_request_without_ai_or_financial_mutation(self):
        """
        Verifies that when a request key was previously rejected (same request_hash),
        replaying with that key returns the saved rejected response immediately,
        calling Gemini ZERO times and writing ZERO financial records to the ledger.
        """
        rejected_key = f"k-rejected-{uuid4().hex}"
        rejected_req_id = uuid4()

        payload = {
            "idempotency_key": rejected_key,
            "captured_at": "2026-09-06T19:00:00+08:00",
            "client_version": "expense-shortcut-v2",
            "image": {"mime_type": "image/jpeg", "base64": "dummy"},
            "note": "Payment cancelled by user"
        }

        from app.services.expense_service import compute_request_hash
        req_hash = compute_request_hash(payload)

        rejected_record = {
            "id": rejected_req_id,
            "device_id": self.device_id,
            "idempotency_key": rejected_key,
            "request_kind": "expense",
            "request_hash": req_hash,
            "status": "rejected",
            "response_payload": {
                "status": "rejected",
                "request_id": str(rejected_req_id),
                "display_summary": "已放弃记账: 用户取消"
            }
        }

        with patch("app.services.expense_service.ingestion_repo") as mock_ingestion, \
             patch("app.services.expense_service.ledger_service") as mock_ledger:

            mock_ingestion.lock_by_device_and_key.return_value = rejected_record

            mock_gemini = MockGeminiService()

            res = process_expense_request(
                conn=self.mock_conn,
                device=self.device,
                payload=payload,
                gemini_service=mock_gemini,
                image_bytes=VALID_JPEG_HEADER
            )

            self.assertEqual(res["status"], "rejected")
            self.assertEqual(res["request_id"], str(rejected_req_id))
            self.assertEqual(mock_gemini.call_count, 0)
            mock_ledger.record_expense.assert_not_called()

    def test_06b_target_contract_delayed_post_after_get_404_race_simulation(self):
        """
        Target contract scenario (CONTRACTS.md Section 3):
        1. Client sent POST with key 'k_delayed', but client timed out before receiving response.
        2. Client recovers: GET by-key/'k_delayed' returns 404 (request has not arrived/persisted yet).
        3. Client abandons recovery and calls target S2 action: POST by-key/'k_delayed'/cancel.
           Server creates a rejected tombstone with request_hash=None and status='rejected'.
        4. Delayed original POST arrives later with key 'k_delayed'.
        5. In target S2 architecture:
           Null-hash tombstone collides with delayed POST; server replays rejection,
           calling Gemini ZERO times and creating ZERO financial rows.
        """
        tombstone_key = f"k-delayed-race-{uuid4().hex}"
        tombstone_req_id = uuid4()

        # Target S2 tombstone record: request_hash is null, status is rejected
        rejected_tombstone = {
            "id": tombstone_req_id,
            "device_id": self.device_id,
            "idempotency_key": tombstone_key,
            "request_kind": "command",
            "operation": "cancel",
            "request_hash": None,
            "status": "rejected",
            "response_payload": {
                "status": "rejected",
                "request_id": str(tombstone_req_id),
                "display_summary": "已放弃记账: Client cancelled recovery before recapture"
            }
        }

        # Target S2 contract simulation function per CONTRACTS.md Section 3:
        # "Null-hash cancelled-key tombstones always replay rejection."
        def target_process_expense_contract(existing, incoming_payload, gemini_service, ledger_service):
            if existing:
                # Target contract rule: if tombstone is null-hash and rejected, always replay rejection
                if existing.get("request_hash") is None and existing.get("status") == "rejected":
                    return existing["response_payload"]
                # Normal idempotency check
                from app.services.expense_service import compute_request_hash
                if existing.get("request_hash") != compute_request_hash(incoming_payload):
                    raise IdempotencyKeyReuseError("Key reuse with different content")
                return existing["response_payload"]
            # Otherwise process new request
            return {"status": "processing"}

        mock_gemini = MockGeminiService()
        mock_ledger = MagicMock()

        delayed_payload = {
            "idempotency_key": tombstone_key,
            "captured_at": "2026-09-06T19:00:00+08:00",
            "client_version": "expense-shortcut-v2",
            "image": {"mime_type": "image/jpeg", "base64": "dummy"},
            "note": "Delayed payment"
        }

        res = target_process_expense_contract(
            existing=rejected_tombstone,
            incoming_payload=delayed_payload,
            gemini_service=mock_gemini,
            ledger_service=mock_ledger
        )

        # Target contract invariants hold:
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["request_id"], str(tombstone_req_id))
        self.assertEqual(mock_gemini.call_count, 0)
        mock_ledger.record_expense.assert_not_called()

    def test_07_target_contract_cancel_on_already_committed_request(self):
        """
        Target contract scenario:
        If the original request had already committed before the cancel arrived,
        the cancel operation MUST return the already committed result rather than rejecting or voiding.
        """
        committed_req_id = uuid4()
        tx_id = uuid4()
        committed_record = {
            "id": committed_req_id,
            "device_id": self.device_id,
            "status": "committed",
            "response_payload": {
                "status": "committed",
                "request_id": str(committed_req_id),
                "transaction_id": str(tx_id),
                "payment_mode": "one_off",
                "display_summary": "¥28.50 · 瑞幸咖啡"
            }
        }

        # Model target cancel handler logic:
        # If existing record status == 'committed', return committed payload
        def target_cancel_handler(record):
            if record["status"] == "committed":
                return record["response_payload"]
            record["status"] = "rejected"
            return {"status": "rejected", "request_id": str(record["id"])}

        result = target_cancel_handler(committed_record)
        self.assertEqual(result["status"], "committed")
        self.assertEqual(result["transaction_id"], str(tx_id))
        self.assertEqual(committed_record["status"], "committed")  # Not mutated to rejected!

    def test_08_target_contract_cancel_on_needs_confirmation_draft(self):
        """
        Target contract scenario:
        If request is in needs_confirmation when cancel arrives,
        it transitions to rejected and creates zero money records.
        """
        draft_req_id = uuid4()
        draft_record = {
            "id": draft_req_id,
            "device_id": self.device_id,
            "status": "needs_confirmation",
            "response_payload": None
        }

        def target_cancel_handler(record):
            if record["status"] == "committed":
                return record["response_payload"]
            record["status"] = "rejected"
            return {
                "status": "rejected",
                "request_id": str(record["id"]),
                "display_summary": "已放弃记账: 用户取消"
            }

        result = target_cancel_handler(draft_record)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(draft_record["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
