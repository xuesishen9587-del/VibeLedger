import os
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test_user:test_pass@127.0.0.1:5432/vibeledger_test")
os.environ.setdefault("DB_SCHEMA", "vibeledger_test_runner")

import io
import unittest
from typing import Any
from uuid import uuid4
from decimal import Decimal
from datetime import datetime, date, timezone
import pypdf
from fastapi.testclient import TestClient

from app.main import create_app
from app.db import get_connection
from app.api.deps import get_db_connection
from app.services.reference_fx_service import ReferenceFxService
from app.domain.statements import ParsedStatementLine, StatementExtractionResult
from app.services.statement_parser import MockStatementParser
from app.api.routes.statements import router as statements_router
from app.api.routes.reconciliation import router as reconciliation_router, candidates_router as reconciliation_candidates_router
from tests.support.db_helper import BaseDbTestCase
import app.repositories.reconciliation as reconciliation_repo
import app.repositories.transactions as tx_repo


def make_pdf_bytes(text_lines: list[str]) -> bytes:
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


class TestReconciliationReviewApiDb(BaseDbTestCase):
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

        cls.mock_fx = ReferenceFxService(fixed_rates={
            ("USD", "CNY"): Decimal("7.20"),
            ("EUR", "CNY"): Decimal("7.80"),
        })
        statements_router._reference_fx_service = cls.mock_fx
        reconciliation_router._reference_fx_service = cls.mock_fx
        reconciliation_candidates_router._reference_fx_service = cls.mock_fx

    def seed_test_data(self):
        import hashlib
        import app.repositories.accounts as accounts_repo
        import app.repositories.devices as devices_repo
        from app.db import transaction

        self.household_id = uuid4()
        self.user_id = uuid4()
        self.device_id = uuid4()
        self.raw_token = f"vbl_test_{uuid4().hex}"
        self.token_hash = hashlib.sha256(self.raw_token.encode("utf-8")).digest()
        self.headers = {"Authorization": f"Bearer {self.raw_token}"}

        # Household B
        self.household_b_id = uuid4()
        self.user_b_id = uuid4()
        self.device_b_id = uuid4()
        self.raw_token_b = f"vbl_test_{uuid4().hex}"
        self.token_b_hash = hashlib.sha256(self.raw_token_b.encode("utf-8")).digest()
        self.headers_b = {"Authorization": f"Bearer {self.raw_token_b}"}

        self.acc_cny_id = uuid4()
        self.acc_savings_id = uuid4()
        self.acc_b_id = uuid4()
        self.cat_dining_id = uuid4()
        self.cat_transport_id = uuid4()

        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                # Household A
                accounts_repo.create_household(conn, self.household_id, "Household A", date(2026, 1, 1), "CNY")
                accounts_repo.create_user(conn, self.user_id, "user_a", "User A", "user_a@test.local", "CNY")
                accounts_repo.add_user_to_household(conn, self.household_id, self.user_id, role="owner")
                devices_repo.create_device(conn, self.device_id, self.user_id, "iPhone A", self.token_hash)

                # Household B
                accounts_repo.create_household(conn, self.household_b_id, "Household B", date(2026, 1, 1), "CNY")
                accounts_repo.create_user(conn, self.user_b_id, "user_b", "User B", "user_b@test.local", "CNY")
                accounts_repo.add_user_to_household(conn, self.household_b_id, self.user_b_id, role="owner")
                devices_repo.create_device(conn, self.device_b_id, self.user_b_id, "iPhone B", self.token_b_hash)

                # Accounts
                accounts_repo.create_account(
                    conn=conn,
                    account_id=self.acc_cny_id,
                    household_id=self.household_id,
                    name="CMB Checking",
                    account_type="cash",
                    currency="CNY",
                    owner_user_id=self.user_id
                )
                accounts_repo.create_account(
                    conn=conn,
                    account_id=self.acc_savings_id,
                    household_id=self.household_id,
                    name="CMB Savings",
                    account_type="savings",
                    currency="CNY",
                    owner_user_id=self.user_id
                )
                accounts_repo.create_account(
                    conn=conn,
                    account_id=self.acc_b_id,
                    household_id=self.household_b_id,
                    name="Household B Account",
                    account_type="cash",
                    currency="CNY",
                    owner_user_id=self.user_b_id
                )

                # Categories
                accounts_repo.create_category(
                    conn=conn,
                    category_id=self.cat_dining_id,
                    household_id=self.household_id,
                    name="Dining",
                    category_type="expense"
                )
                accounts_repo.create_category(
                    conn=conn,
                    category_id=self.cat_transport_id,
                    household_id=self.household_id,
                    name="Transport",
                    category_type="expense"
                )
        finally:
            conn.close()

    def _upload_test_statement(self, lines: list[ParsedStatementLine], closing_balance: Decimal = None, default_expense_category_id: Any = None):
        pdf_bytes = make_pdf_bytes(["Test Statement Document"])
        mock_extraction = StatementExtractionResult(
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            closing_balance=closing_balance,
            currency="CNY",
            lines=lines
        )
        statements_router._statement_parser = MockStatementParser(result=mock_extraction)

        data = {}
        target_cat = self.cat_dining_id if default_expense_category_id is None else (default_expense_category_id if default_expense_category_id is not False else None)
        if target_cat is not None:
            data["default_expense_category_id"] = str(target_cat)

        res = self.client.post(
            f"/api/v1/accounts/{self.acc_cny_id}/statements",
            headers=self.headers,
            files={"file": ("statement.pdf", pdf_bytes, "application/pdf")},
            data=data
        )
        self.assertEqual(res.status_code, 201)
        return res.json()["batch_id"]

    def test_get_statement_batch_and_preview(self):
        lines = [
            ParsedStatementLine(
                source_page_no=1,
                source_row_no=1,
                transaction_on=date(2026, 7, 5),
                description_raw="Restaurant Lunch",
                amount=Decimal("120.00"),
                currency="CNY",
                direction="debit",
                line_type="expense"
            ),
            ParsedStatementLine(
                source_page_no=1,
                source_row_no=2,
                transaction_on=date(2026, 7, 10),
                description_raw="Taxi Ride",
                amount=Decimal("45.00"),
                currency="CNY",
                direction="debit",
                line_type="expense"
            )
        ]
        batch_id = self._upload_test_statement(lines, closing_balance=Decimal("-165.00"))

        # 1. Get batch summary
        sum_res = self.client.get(f"/api/v1/reconciliation-batches/{batch_id}", headers=self.headers)
        self.assertEqual(sum_res.status_code, 200)
        sum_data = sum_res.json()
        self.assertEqual(sum_data["status"], "ready")
        self.assertEqual(sum_data["summary"]["line_count"], 2)
        self.assertEqual(sum_data["summary"]["created_count"], 2)

        # 2. Get batch preview
        prev_res = self.client.get(f"/api/v1/reconciliation-batches/{batch_id}/preview", headers=self.headers)
        self.assertEqual(prev_res.status_code, 200)
        prev_data = prev_res.json()
        self.assertIn("batch", prev_data)
        self.assertIn("candidates", prev_data)
        self.assertEqual(len(prev_data["candidates"]), 2)
        self.assertEqual(prev_data["summary"]["line_count"], 2)

    def test_get_statement_lines_filtering_and_isolation(self):
        lines = [
            ParsedStatementLine(
                source_page_no=1,
                source_row_no=1,
                transaction_on=date(2026, 7, 5),
                description_raw="Grocery Supermarket",
                amount=Decimal("200.00"),
                currency="CNY",
                direction="debit",
                line_type="expense"
            ),
            ParsedStatementLine(
                source_page_no=1,
                source_row_no=2,
                transaction_on=date(2026, 7, 8),
                description_raw="Transfer to savings",
                amount=Decimal("500.00"),
                currency="CNY",
                direction="debit",
                line_type="transfer"
            )
        ]
        batch_id = self._upload_test_statement(lines)

        # All lines
        res_all = self.client.get(f"/api/v1/reconciliation-batches/{batch_id}/statement-lines", headers=self.headers)
        self.assertEqual(res_all.status_code, 200)
        self.assertEqual(len(res_all.json()["items"]), 2)

        # Filter by line_type=transfer
        res_tf = self.client.get(f"/api/v1/reconciliation-batches/{batch_id}/statement-lines?line_type=transfer", headers=self.headers)
        self.assertEqual(res_tf.status_code, 200)
        items = res_tf.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["line_type"], "transfer")

        # Cross-household access returns 404
        res_cross = self.client.get(f"/api/v1/reconciliation-batches/{batch_id}/statement-lines", headers=self.headers_b)
        self.assertEqual(res_cross.status_code, 404)

    def test_candidate_accept_workflow(self):
        # Line with missing category -> CATEGORY_REQUIRED candidate
        lines = [
            ParsedStatementLine(
                source_page_no=1,
                source_row_no=1,
                transaction_on=date(2026, 7, 5),
                description_raw="Restaurant Store Charge",
                amount=Decimal("75.00"),
                currency="CNY",
                direction="debit",
                line_type="expense"
            )
        ]
        # Upload without default category so candidate has CATEGORY_REQUIRED
        batch_id = self._upload_test_statement(lines, default_expense_category_id=False)

        conn = get_connection(self.test_schema)
        try:
            candidates = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
            self.assertEqual(len(candidates), 1)
            cand = candidates[0]
            self.assertEqual(cand["status"], "needs_review")
            self.assertEqual(cand["reason_code"], "CATEGORY_REQUIRED")
            cand_id = cand["id"]
        finally:
            conn.close()

        # Batch starts in needs_review
        sum_res = self.client.get(f"/api/v1/reconciliation-batches/{batch_id}", headers=self.headers)
        self.assertEqual(sum_res.json()["status"], "needs_review")

        # 1. Empty accept on CATEGORY_REQUIRED candidate MUST be rejected (422)
        empty_accept_res = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_id}/accept",
            headers=self.headers,
            json={}
        )
        self.assertEqual(empty_accept_res.status_code, 422)

        # 2. PATCH candidate with explicit validated category
        patch_res = self.client.patch(
            f"/api/v1/reconciliation-candidates/{cand_id}",
            headers=self.headers,
            json={
                "payload": {
                    "transaction": {
                        "category_id": str(self.cat_dining_id)
                    }
                }
            }
        )
        self.assertEqual(patch_res.status_code, 200)

        # 3. Accept candidate after validated PATCH
        accept_res = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_id}/accept",
            headers=self.headers,
            json={}
        )
        self.assertEqual(accept_res.status_code, 200)
        data = accept_res.json()
        self.assertEqual(data["status"], "ready")
        self.assertEqual(data["summary"]["pending_count"], 0)

        # Commit batch
        commit_res = self.client.post(f"/api/v1/reconciliation-batches/{batch_id}/commit", headers=self.headers, json={})
        self.assertEqual(commit_res.status_code, 200)
        self.assertEqual(commit_res.json()["status"], "committed")

    def test_candidate_accept_with_target_transaction(self):
        # Create an existing committed transaction
        conn = get_connection(self.test_schema)
        tx_id = uuid4()
        try:
            tx_repo.create_transaction(
                conn=conn,
                tx_id=tx_id,
                household_id=self.household_id,
                transaction_type="expense",
                occurred_on=date(2026, 7, 10),
                original_amount=Decimal("50.00"),
                original_currency="CNY",
                from_amount=Decimal("50.00"),
                from_currency="CNY",
                from_account_id=self.acc_cny_id,
                category_id=self.cat_dining_id,
                status="committed",
                source="shortcut"
            )
            conn.commit()
        finally:
            conn.close()

        # Statement line with slightly different description
        lines = [
            ParsedStatementLine(
                source_page_no=1,
                source_row_no=1,
                transaction_on=date(2026, 7, 10),
                description_raw="Restaurant Dining Pos 99",
                amount=Decimal("50.00"),
                currency="CNY",
                direction="debit",
                line_type="expense"
            )
        ]
        batch_id = self._upload_test_statement(lines)

        conn = get_connection(self.test_schema)
        try:
            candidates = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
            cand_id = candidates[0]["id"]
        finally:
            conn.close()

        # Accept candidate with explicit target_transaction_id
        accept_res = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_id}/accept",
            headers=self.headers,
            json={"target_transaction_id": str(tx_id)}
        )
        self.assertEqual(accept_res.status_code, 200)

        # Commit batch
        commit_res = self.client.post(f"/api/v1/reconciliation-batches/{batch_id}/commit", headers=self.headers, json={})
        self.assertEqual(commit_res.status_code, 200)
        self.assertEqual(commit_res.json()["summary"]["matched_count"], 1)

        # Check that existing transaction is now statement_confirmed
        conn = get_connection(self.test_schema)
        try:
            tx = tx_repo.get_transaction(conn, tx_id)
            self.assertEqual(tx["verification_status"], "statement_confirmed")
            self.assertEqual(str(tx["statement_batch_id"]), str(batch_id))
        finally:
            conn.close()

    def test_candidate_patch_edit_workflow(self):
        lines = [
            ParsedStatementLine(
                source_page_no=1,
                source_row_no=1,
                transaction_on=date(2026, 7, 15),
                description_raw="City Subway Transport",
                amount=Decimal("6.00"),
                currency="CNY",
                direction="debit",
                line_type="expense"
            )
        ]
        # Upload without default category so candidate starts in needs_review (CATEGORY_REQUIRED)
        batch_id = self._upload_test_statement(lines, default_expense_category_id=False)

        conn = get_connection(self.test_schema)
        try:
            candidates = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
            cand_id = candidates[0]["id"]
            self.assertEqual(candidates[0]["status"], "needs_review")
            self.assertEqual(candidates[0]["reason_code"], "CATEGORY_REQUIRED")
        finally:
            conn.close()

        # 1. needs_review candidate -> safe PATCH with new category (Transport) -> 200
        patch_res = self.client.patch(
            f"/api/v1/reconciliation-candidates/{cand_id}",
            headers=self.headers,
            json={"payload": {"transaction": {"category_id": str(self.cat_transport_id)}}}
        )
        self.assertEqual(patch_res.status_code, 200)

        # Verify candidate payload in DB and reason_code cleared
        conn = get_connection(self.test_schema)
        try:
            candidates = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
            cand = next(c for c in candidates if c["id"] == cand_id)
            self.assertEqual(cand["payload"]["transaction"]["category_id"], str(self.cat_transport_id))
            self.assertIsNone(cand["reason_code"])
        finally:
            conn.close()

        # 2. Accept candidate -> 200
        accept_res = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_id}/accept",
            headers=self.headers,
            json={}
        )
        self.assertEqual(accept_res.status_code, 200)

        # 3. Accepted candidate -> generic PATCH category => 422
        res_patch_cat_accepted = self.client.patch(
            f"/api/v1/reconciliation-candidates/{cand_id}",
            headers=self.headers,
            json={"payload": {"transaction": {"category_id": str(self.cat_dining_id)}}}
        )
        self.assertEqual(res_patch_cat_accepted.status_code, 422)

        # 4. Accepted candidate -> generic PATCH amount => 422
        res_patch_amt_accepted = self.client.patch(
            f"/api/v1/reconciliation-candidates/{cand_id}",
            headers=self.headers,
            json={"payload": {"transaction": {"amount": "10.00"}}}
        )
        self.assertEqual(res_patch_amt_accepted.status_code, 422)

        # 5. Commit batch -> 200
        commit_res = self.client.post(f"/api/v1/reconciliation-batches/{batch_id}/commit", headers=self.headers, json={})
        self.assertEqual(commit_res.status_code, 200)

        # 6. Verify created transaction has Transport category
        conn = get_connection(self.test_schema)
        try:
            txs, _ = tx_repo.list_transactions_with_filters(conn, self.household_id, account_id=self.acc_cny_id)
            created_tx = next(t for t in txs if t["original_amount"] == Decimal("6.00"))
            self.assertEqual(str(created_tx["category"]["id"]), str(self.cat_transport_id))
        finally:
            conn.close()

    def test_accepted_transfer_candidate_patch_freeze(self):
        """
        Regression proving that accepted transfer candidate rejects PATCH (account/amount mutation) with 422.
        """
        lines = [
            ParsedStatementLine(
                source_page_no=1,
                source_row_no=1,
                transaction_on=date(2026, 7, 18),
                description_raw="ATM Cash Outflow Leg",
                amount=Decimal("150.00"),
                currency="CNY",
                direction="debit",
                line_type="unknown"
            )
        ]
        batch_id = self._upload_test_statement(lines, default_expense_category_id=False)

        conn = get_connection(self.test_schema)
        try:
            candidates = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
            cand_id = candidates[0]["id"]
        finally:
            conn.close()

        # Resolve as valid debit transfer
        res_resolve = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_id}/resolve",
            headers=self.headers,
            json={"resolution_type": "transfer", "counter_account_id": str(self.acc_savings_id)}
        )
        self.assertEqual(res_resolve.status_code, 200)

        # Accept candidate
        res_accept = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_id}/accept",
            headers=self.headers,
            json={}
        )
        self.assertEqual(res_accept.status_code, 200)

        # Accepted transfer candidate -> PATCH account => 422
        res_patch_acc = self.client.patch(
            f"/api/v1/reconciliation-candidates/{cand_id}",
            headers=self.headers,
            json={"payload": {"transfer": {"to_account_id": str(self.acc_b_id)}}}
        )
        self.assertEqual(res_patch_acc.status_code, 422)

        # Accepted transfer candidate -> PATCH amount => 422
        res_patch_amt = self.client.patch(
            f"/api/v1/reconciliation-candidates/{cand_id}",
            headers=self.headers,
            json={"payload": {"transfer": {"to_amount": "200.00"}}}
        )
        self.assertEqual(res_patch_amt.status_code, 422)

    def test_candidate_reject_workflow(self):
        lines = [
            ParsedStatementLine(
                source_page_no=1,
                source_row_no=1,
                transaction_on=date(2026, 7, 20),
                description_raw="Private Personal Expense Not For Ledger",
                amount=Decimal("300.00"),
                currency="CNY",
                direction="debit",
                line_type="expense"
            )
        ]
        batch_id = self._upload_test_statement(lines)

        conn = get_connection(self.test_schema)
        try:
            candidates = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
            cand_id = candidates[0]["id"]
            line_id = candidates[0]["statement_line_id"]
        finally:
            conn.close()

        # Reject candidate
        reject_res = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_id}/reject",
            headers=self.headers,
            json={"reason": "Private item excluded by user"}
        )
        self.assertEqual(reject_res.status_code, 200)
        data = reject_res.json()
        self.assertEqual(data["summary"]["created_count"], 0)

        # Verify candidate and statement line in DB
        conn = get_connection(self.test_schema)
        try:
            candidates = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
            cand = next(c for c in candidates if c["id"] == cand_id)
            self.assertEqual(cand["status"], "rejected")
            self.assertEqual(cand["reason_detail"], "Private item excluded by user")

            lines = reconciliation_repo.list_statement_lines_for_batch(conn, batch_id)
            stmt_line = next(l for l in lines if l["id"] == line_id)
            self.assertEqual(stmt_line["match_status"], "ignored")
            # Evidence is retained
            self.assertEqual(stmt_line["description_raw"], "Private Personal Expense Not For Ledger")
        finally:
            conn.close()

        # Commit batch
        commit_res = self.client.post(f"/api/v1/reconciliation-batches/{batch_id}/commit", headers=self.headers, json={})
        self.assertEqual(commit_res.status_code, 200)
        self.assertEqual(commit_res.json()["summary"]["created_count"], 0)

        # Verify 0 transactions created in ledger
        conn = get_connection(self.test_schema)
        try:
            txs, _ = tx_repo.list_transactions_with_filters(conn, self.household_id, account_id=self.acc_cny_id)
            self.assertEqual(len(txs), 0)
        finally:
            conn.close()

    def test_batch_commit_optimistic_concurrency_conflict(self):
        lines = [
            ParsedStatementLine(
                source_page_no=1,
                source_row_no=1,
                transaction_on=date(2026, 7, 5),
                description_raw="Snack",
                amount=Decimal("12.00"),
                currency="CNY",
                direction="debit",
                line_type="expense"
            )
        ]
        batch_id = self._upload_test_statement(lines)

        conn = get_connection(self.test_schema)
        try:
            candidates = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
            cand_id = candidates[0]["id"]
        finally:
            conn.close()

        # Mutate candidate to increment row_version
        self.client.patch(
            f"/api/v1/reconciliation-candidates/{cand_id}",
            headers=self.headers,
            json={"payload": {"transaction": {"merchant": "Convenience Store"}}}
        )

        # Attempt commit with stale row_version (0)
        commit_stale = self.client.post(
            f"/api/v1/reconciliation-batches/{batch_id}/commit",
            headers=self.headers,
            json={"row_version": 0}
        )
        self.assertEqual(commit_stale.status_code, 409)
        err = commit_stale.json()["error"]
        self.assertEqual(err["code"], "BATCH_VERSION_CONFLICT")
        self.assertTrue(err["retryable"])

        # Fetch current batch row_version
        sum_res = self.client.get(f"/api/v1/reconciliation-batches/{batch_id}", headers=self.headers)
        current_ver = sum_res.json()["row_version"]

        # Commit with correct row_version
        commit_ok = self.client.post(
            f"/api/v1/reconciliation-batches/{batch_id}/commit",
            headers=self.headers,
            json={"row_version": current_ver}
        )
        self.assertEqual(commit_ok.status_code, 200)
        self.assertEqual(commit_ok.json()["status"], "committed")


if __name__ == "__main__":
    unittest.main()
