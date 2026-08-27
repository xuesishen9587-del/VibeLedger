import os
import io
import unittest
from uuid import uuid4
from decimal import Decimal
from datetime import datetime, date, timezone
import pypdf
from fastapi.testclient import TestClient

from app.main import create_app
from app.db import get_connection, transaction
from app.api.deps import get_db_connection
from app.services.reference_fx_service import ReferenceFxService
from app.domain.statements import ParsedStatementLine, StatementExtractionResult
from app.services.statement_parser import MockStatementParser
from app.api.routes.statements import router as statements_router
from app.api.routes.reconciliation import router as reconciliation_router, candidates_router as reconciliation_candidates_router
from tests.support.db_helper import BaseDbTestCase
import app.repositories.reconciliation as reconciliation_repo
import app.repositories.transactions as tx_repo


def make_pdf_bytes(text_lines: list[str], password: str = None) -> bytes:
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

    if password:
        writer.encrypt(password)

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class TestStatementApiDb(BaseDbTestCase):
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

        # Household B for cross-household isolation
        self.household_b_id = uuid4()
        self.user_b_id = uuid4()
        self.device_b_id = uuid4()
        self.raw_token_b = f"vbl_test_{uuid4().hex}"
        self.token_b_hash = hashlib.sha256(self.raw_token_b.encode("utf-8")).digest()
        self.headers_b = {"Authorization": f"Bearer {self.raw_token_b}"}

        self.acc_cny_id = uuid4()
        self.acc_inactive_id = uuid4()
        self.acc_invest_id = uuid4()
        self.acc_b_id = uuid4()
        self.cat_dining_id = uuid4()
        self.cat_income_id = uuid4()

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
                    account_id=self.acc_inactive_id,
                    household_id=self.household_id,
                    name="Old Inactive Account",
                    account_type="cash",
                    currency="CNY",
                    owner_user_id=self.user_id,
                    status="inactive"
                )
                accounts_repo.create_account(
                    conn=conn,
                    account_id=self.acc_invest_id,
                    household_id=self.household_id,
                    name="Investment Portfolio",
                    account_type="investment",
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
                    category_id=self.cat_income_id,
                    household_id=self.household_id,
                    name="Salary",
                    category_type="income"
                )
        finally:
            conn.close()

    def test_statement_upload_valid_cny_creates_batch_and_candidates(self):
        pdf_bytes = make_pdf_bytes(["Statement for July 2026", "Expense 100.00 CNY", "Income 5000.00 CNY"])

        mock_extraction = StatementExtractionResult(
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            closing_balance=Decimal("4900.00"),
            currency="CNY",
            lines=[
                ParsedStatementLine(
                    source_page_no=1,
                    source_row_no=1,
                    transaction_on=date(2026, 7, 10),
                    description_raw="Restaurant Lunch",
                    amount=Decimal("100.00"),
                    currency="CNY",
                    direction="debit",
                    line_type="expense",
                    merchant_hint="Restaurant"
                ),
                ParsedStatementLine(
                    source_page_no=1,
                    source_row_no=2,
                    transaction_on=date(2026, 7, 25),
                    description_raw="Monthly Salary",
                    amount=Decimal("5000.00"),
                    currency="CNY",
                    direction="credit",
                    line_type="income"
                )
            ],
            parser_version="mock-statement-v1.0"
        )
        statements_router._statement_parser = MockStatementParser(result=mock_extraction)

        response = self.client.post(
            f"/api/v1/accounts/{self.acc_cny_id}/statements",
            headers=self.headers,
            files={"file": ("statement.pdf", pdf_bytes, "application/pdf")},
            data={"default_expense_category_id": str(self.cat_dining_id)}
        )

        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertIn("batch_id", data)
        self.assertEqual(data["batch_type"], "statement")
        self.assertEqual(data["summary"]["line_count"], 2)
        batch_id = data["batch_id"]

        # Verify DB records
        conn = get_connection(self.test_schema)
        try:
            batch = reconciliation_repo.get_reconciliation_batch(conn, batch_id)
            self.assertIsNotNone(batch)
            self.assertEqual(batch["account_id"], self.acc_cny_id)
            self.assertEqual(batch["parser_version"], "mock-statement-v1.0")
            self.assertEqual(batch["authoritative_balance"], Decimal("4900.00"))

            lines = reconciliation_repo.list_statement_lines_for_batch(conn, batch_id)
            self.assertEqual(len(lines), 2)

            candidates = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
            self.assertGreaterEqual(len(candidates), 2)
        finally:
            conn.close()

    def test_statement_upload_account_fixed_by_url(self):
        """
        Account context is strictly pinned by the route parameter; PDF text cannot divert account.
        """
        pdf_bytes = make_pdf_bytes(["HSBC Bank Statement for John Doe"])
        mock_extraction = StatementExtractionResult(
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            closing_balance=Decimal("200.00"),
            currency="CNY",
            lines=[
                ParsedStatementLine(
                    source_page_no=1,
                    source_row_no=1,
                    transaction_on=date(2026, 7, 5),
                    description_raw="Coffee",
                    amount=Decimal("30.00"),
                    currency="CNY",
                    direction="debit",
                    line_type="expense"
                )
            ]
        )
        statements_router._statement_parser = MockStatementParser(result=mock_extraction)

        response = self.client.post(
            f"/api/v1/accounts/{self.acc_cny_id}/statements",
            headers=self.headers,
            files={"file": ("statement.pdf", pdf_bytes, "application/pdf")}
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["account_id"], str(self.acc_cny_id))

    def test_statement_upload_cross_household_rejected(self):
        pdf_bytes = make_pdf_bytes(["Some Statement"])
        statements_router._statement_parser = MockStatementParser()

        response = self.client.post(
            f"/api/v1/accounts/{self.acc_b_id}/statements",
            headers=self.headers,  # Household A token for Household B account
            files={"file": ("statement.pdf", pdf_bytes, "application/pdf")}
        )
        self.assertEqual(response.status_code, 404)
        err = response.json()
        self.assertEqual(err["error"]["code"], "ACCOUNT_NOT_FOUND")

    def test_statement_upload_inactive_account_rejected(self):
        pdf_bytes = make_pdf_bytes(["Some Statement"])
        statements_router._statement_parser = MockStatementParser()

        response = self.client.post(
            f"/api/v1/accounts/{self.acc_inactive_id}/statements",
            headers=self.headers,
            files={"file": ("statement.pdf", pdf_bytes, "application/pdf")}
        )
        self.assertEqual(response.status_code, 422)
        err = response.json()
        self.assertEqual(err["error"]["code"], "ACCOUNT_INACTIVE")

    def test_statement_upload_investment_account_supported(self):
        pdf_bytes = make_pdf_bytes(["Investment Statement"])
        statements_router._statement_parser = MockStatementParser()

        response = self.client.post(
            f"/api/v1/accounts/{self.acc_invest_id}/statements",
            headers=self.headers,
            files={"file": ("statement.pdf", pdf_bytes, "application/pdf")}
        )
        self.assertEqual(response.status_code, 201)
        res = response.json()
        self.assertIn("batch_id", res)
        self.assertEqual(res["status"], "ready")

    def test_statement_upload_invalid_file_rejected(self):
        response = self.client.post(
            f"/api/v1/accounts/{self.acc_cny_id}/statements",
            headers=self.headers,
            files={"file": ("statement.txt", b"plain text is not pdf", "text/plain")}
        )
        self.assertEqual(response.status_code, 400)
        err = response.json()
        self.assertEqual(err["error"]["code"], "STATEMENT_PARSE_FAILED")

    def test_password_sentinel_never_persisted(self):
        """
        Verifies that PDF decryption passwords are never stored in any database column or audit event.
        """
        password_sentinel = "TOP_SECRET_SENTINEL_PASSWORD_998877"
        pdf_bytes = make_pdf_bytes(["Encrypted Bank Statement"], password=password_sentinel)

        mock_extraction = StatementExtractionResult(
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            closing_balance=Decimal("100.00"),
            currency="CNY",
            lines=[
                ParsedStatementLine(
                    source_page_no=1,
                    source_row_no=1,
                    transaction_on=date(2026, 7, 10),
                    description_raw="Store Purchase",
                    amount=Decimal("50.00"),
                    currency="CNY",
                    direction="debit",
                    line_type="expense"
                )
            ]
        )
        statements_router._statement_parser = MockStatementParser(result=mock_extraction)

        response = self.client.post(
            f"/api/v1/accounts/{self.acc_cny_id}/statements",
            headers=self.headers,
            files={"file": ("encrypted.pdf", pdf_bytes, "application/pdf")},
            data={"password": password_sentinel}
        )
        self.assertEqual(response.status_code, 201)

        # Inspect database tables
        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                # Search reconciliation_batches
                cur.execute("SELECT * FROM reconciliation_batches WHERE failure_detail LIKE %s OR parser_version LIKE %s;", (f"%{password_sentinel}%", f"%{password_sentinel}%"))
                self.assertEqual(len(cur.fetchall()), 0)

                # Search reconciliation_candidates
                cur.execute("SELECT * FROM reconciliation_candidates WHERE payload::text LIKE %s OR reason_detail LIKE %s;", (f"%{password_sentinel}%", f"%{password_sentinel}%"))
                self.assertEqual(len(cur.fetchall()), 0)

                # Search audit_events
                cur.execute("SELECT * FROM audit_events WHERE after_data::text LIKE %s OR before_data::text LIKE %s;", (f"%{password_sentinel}%", f"%{password_sentinel}%"))
                self.assertEqual(len(cur.fetchall()), 0)
        finally:
            conn.close()

    def test_missing_authoritative_balance_null_residual(self):
        """
        When closing_balance is absent:
        - authoritative_balance remains NULL
        - residual_amount remains NULL
        - line matching still functions
        - no adjustment candidate is created
        """
        pdf_bytes = make_pdf_bytes(["Activity Statement without closing balance"])
        mock_extraction = StatementExtractionResult(
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            closing_balance=None,  # Missing closing balance
            currency="CNY",
            lines=[
                ParsedStatementLine(
                    source_page_no=1,
                    source_row_no=1,
                    transaction_on=date(2026, 7, 12),
                    description_raw="Snack Store",
                    amount=Decimal("15.00"),
                    currency="CNY",
                    direction="debit",
                    line_type="expense"
                )
            ]
        )
        statements_router._statement_parser = MockStatementParser(result=mock_extraction)

        response = self.client.post(
            f"/api/v1/accounts/{self.acc_cny_id}/statements",
            headers=self.headers,
            files={"file": ("statement.pdf", pdf_bytes, "application/pdf")},
            data={"default_expense_category_id": str(self.cat_dining_id)}
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        batch_id = data["batch_id"]
        self.assertIsNone(data["summary"]["residual_amount"])

        conn = get_connection(self.test_schema)
        try:
            batch = reconciliation_repo.get_reconciliation_batch(conn, batch_id)
            self.assertIsNone(batch["authoritative_balance"])
            self.assertIsNone(batch["residual_amount"])
            self.assertIsNone(batch["adjustment_amount"])

            candidates = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
            # Ensure no adjustment candidate was created
            adj_cands = [c for c in candidates if c["candidate_type"] == "adjustment"]
            self.assertEqual(len(adj_cands), 0)
        finally:
            conn.close()

    def test_repeated_statement_upload_replay_safety(self):
        """
        Uploading the same statement twice creates two distinct batches.
        After committing batch 1, batch 2 re-matches the committed facts and creates 0 duplicate transactions.
        """
        pdf_bytes = make_pdf_bytes(["Statement July 2026"])
        mock_extraction = StatementExtractionResult(
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            closing_balance=Decimal("-50.00"),
            currency="CNY",
            lines=[
                ParsedStatementLine(
                    source_page_no=1,
                    source_row_no=1,
                    transaction_on=date(2026, 7, 15),
                    description_raw="Pharmacy Medicine",
                    amount=Decimal("50.00"),
                    currency="CNY",
                    direction="debit",
                    line_type="expense",
                    merchant_hint="Pharmacy"
                )
            ]
        )
        statements_router._statement_parser = MockStatementParser(result=mock_extraction)

        # Upload Batch 1
        res1 = self.client.post(
            f"/api/v1/accounts/{self.acc_cny_id}/statements",
            headers=self.headers,
            files={"file": ("statement.pdf", pdf_bytes, "application/pdf")},
            data={"default_expense_category_id": str(self.cat_dining_id)}
        )
        self.assertEqual(res1.status_code, 201)
        batch1_id = res1.json()["batch_id"]

        # Commit Batch 1
        commit1 = self.client.post(
            f"/api/v1/reconciliation-batches/{batch1_id}/commit",
            headers=self.headers,
            json={}
        )
        self.assertEqual(commit1.status_code, 200)
        self.assertEqual(commit1.json()["summary"]["created_count"], 1)

        # Upload Batch 2 (same statement)
        res2 = self.client.post(
            f"/api/v1/accounts/{self.acc_cny_id}/statements",
            headers=self.headers,
            files={"file": ("statement.pdf", pdf_bytes, "application/pdf")},
            data={"default_expense_category_id": str(self.cat_dining_id)}
        )
        self.assertEqual(res2.status_code, 201)
        batch2_id = res2.json()["batch_id"]
        self.assertNotEqual(batch1_id, batch2_id)

        # Batch 2 matched the transaction created by Batch 1
        data2 = res2.json()
        self.assertEqual(data2["summary"]["matched_count"], 1)
        self.assertEqual(data2["summary"]["created_count"], 0)

        # Commit Batch 2
        commit2 = self.client.post(
            f"/api/v1/reconciliation-batches/{batch2_id}/commit",
            headers=self.headers,
            json={}
        )
        self.assertEqual(commit2.status_code, 200)
        self.assertEqual(commit2.json()["summary"]["matched_count"], 1)
        self.assertEqual(commit2.json()["summary"]["created_count"], 0)


    def test_ambiguous_match_options_and_review_workflow(self):
        """
        Tests ambiguous candidate review:
        1. Two plausible transactions exist.
        2. Matcher flags MULTIPLE_TRANSACTION_MATCHES and includes options in preview.
        3. Accept without target is rejected.
        4. Accept with invalid / cross-household target is rejected.
        5. Accept with valid selected target transaction succeeds.
        6. Batch row_version increments and commit succeeds.
        """
        # Create two similar transactions
        tx_a_id = uuid4()
        tx_b_id = uuid4()
        tx_cross_id = uuid4()
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                tx_repo.create_transaction(
                    conn, tx_a_id, self.household_id, "expense", date(2026, 7, 20),
                    original_amount=Decimal("199.00"), original_currency="CNY",
                    from_amount=Decimal("199.00"), from_currency="CNY",
                    from_account_id=self.acc_cny_id, category_id=self.cat_dining_id,
                    merchant="Starbucks Coffee", status="committed"
                )
                tx_repo.create_transaction(
                    conn, tx_b_id, self.household_id, "expense", date(2026, 7, 21),
                    original_amount=Decimal("199.00"), original_currency="CNY",
                    from_amount=Decimal("199.00"), from_currency="CNY",
                    from_account_id=self.acc_cny_id, category_id=self.cat_dining_id,
                    merchant="Starbucks Coffee", status="committed"
                )
                # Cross-household transaction
                tx_repo.create_transaction(
                    conn, tx_cross_id, self.household_b_id, "expense", date(2026, 7, 20),
                    original_amount=Decimal("199.00"), original_currency="CNY",
                    from_amount=Decimal("199.00"), from_currency="CNY",
                    from_account_id=self.acc_b_id, category_id=self.cat_dining_id,
                    merchant="Starbucks", status="committed"
                )
        finally:
            conn.close()

        # Upload statement line with ambiguous match
        pdf_bytes = make_pdf_bytes(["Statement Starbucks July 2026"])
        mock_extraction = StatementExtractionResult(
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            closing_balance=Decimal("-199.00"),
            currency="CNY",
            lines=[
                ParsedStatementLine(
                    source_page_no=1,
                    source_row_no=1,
                    transaction_on=date(2026, 7, 20),
                    description_raw="Starbucks Coffee",
                    amount=Decimal("199.00"),
                    currency="CNY",
                    direction="debit",
                    line_type="expense",
                    merchant_hint="Starbucks Coffee"
                )
            ]
        )
        statements_router._statement_parser = MockStatementParser(result=mock_extraction)

        res_upload = self.client.post(
            f"/api/v1/accounts/{self.acc_cny_id}/statements",
            headers=self.headers,
            files={"file": ("statement.pdf", pdf_bytes, "application/pdf")},
            data={"default_expense_category_id": str(self.cat_dining_id)}
        )
        self.assertEqual(res_upload.status_code, 201)
        batch_id = res_upload.json()["batch_id"]

        # Preview exposes options
        prev_res = self.client.get(f"/api/v1/reconciliation-batches/{batch_id}/preview", headers=self.headers)
        self.assertEqual(prev_res.status_code, 200)
        match_cands = [c for c in prev_res.json()["candidates"] if c["candidate_type"] == "match"]
        self.assertEqual(len(match_cands), 1)
        cand = match_cands[0]
        cand_id = cand["id"]
        self.assertEqual(cand["status"], "needs_review")
        self.assertEqual(cand["reason_code"], "MULTIPLE_TRANSACTION_MATCHES")
        self.assertIn("options", cand)
        self.assertGreaterEqual(len(cand["options"]), 2)

        opt_tx_ids = [opt["transaction_id"] for opt in cand["options"]]
        self.assertIn(str(tx_a_id), opt_tx_ids)
        self.assertIn(str(tx_b_id), opt_tx_ids)

        # 1. Accept without explicit target_transaction_id must be rejected
        accept_no_target = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_id}/accept",
            headers=self.headers,
            json={}
        )
        self.assertEqual(accept_no_target.status_code, 422)

        # 2. Accept with cross-household target must be rejected
        accept_cross = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_id}/accept",
            headers=self.headers,
            json={"target_transaction_id": str(tx_cross_id)}
        )
        self.assertEqual(accept_cross.status_code, 422)

        # 3. Accept with Option A succeeds
        accept_ok = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_id}/accept",
            headers=self.headers,
            json={"target_transaction_id": str(tx_a_id)}
        )
        self.assertEqual(accept_ok.status_code, 200)
        self.assertIn(accept_ok.json()["status"], ("ready", "needs_review"))

        # Refreshed preview reflects accepted candidate and updated batch row_version
        prev_after = self.client.get(f"/api/v1/reconciliation-batches/{batch_id}/preview", headers=self.headers)
        self.assertEqual(prev_after.status_code, 200)
        cand_after = [c for c in prev_after.json()["candidates"] if c["id"] == cand_id][0]
        self.assertEqual(cand_after["status"], "accepted")
        self.assertGreater(prev_after.json()["batch"]["row_version"], 0)

        # Commit batch succeeds
        commit_res = self.client.post(
            f"/api/v1/reconciliation-batches/{batch_id}/commit",
            headers=self.headers,
            json={"row_version": prev_after.json()["batch"]["row_version"]}
        )
        self.assertEqual(commit_res.status_code, 200)
        self.assertEqual(commit_res.json()["status"], "committed")

    def test_candidate_category_patch_and_commit_workflow(self):
        """
        Tests candidate patch workflow:
        1. Statement creates candidate without default category.
        2. Candidate has CATEGORY_REQUIRED reason.
        3. Invalid category patch -> 422.
        4. Valid category patch updates payload and increments batch row_version.
        5. Accept and batch commit succeed.
        """
        pdf_bytes = make_pdf_bytes(["Statement Grocery July 2026"])
        mock_extraction = StatementExtractionResult(
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            closing_balance=Decimal("-88.00"),
            currency="CNY",
            lines=[
                ParsedStatementLine(
                    source_page_no=1,
                    source_row_no=1,
                    transaction_on=date(2026, 7, 25),
                    description_raw="Supermarket Organic",
                    amount=Decimal("88.00"),
                    currency="CNY",
                    direction="debit",
                    line_type="expense"
                )
            ]
        )
        statements_router._statement_parser = MockStatementParser(result=mock_extraction)

        # Upload without default category
        res_upload = self.client.post(
            f"/api/v1/accounts/{self.acc_cny_id}/statements",
            headers=self.headers,
            files={"file": ("statement.pdf", pdf_bytes, "application/pdf")}
        )
        self.assertEqual(res_upload.status_code, 201)
        batch_id = res_upload.json()["batch_id"]

        prev_res = self.client.get(f"/api/v1/reconciliation-batches/{batch_id}/preview", headers=self.headers)
        cand = prev_res.json()["candidates"][0]
        cand_id = cand["id"]
        v_before = prev_res.json()["batch"]["row_version"]

        # 1. Invalid category UUID -> 422
        patch_bad = self.client.patch(
            f"/api/v1/reconciliation-candidates/{cand_id}",
            headers=self.headers,
            json={"payload": {"transaction": {"category_id": str(uuid4())}}}
        )
        self.assertEqual(patch_bad.status_code, 422)

        # 2. Valid category patch -> 200
        patch_ok = self.client.patch(
            f"/api/v1/reconciliation-candidates/{cand_id}",
            headers=self.headers,
            json={"payload": {"transaction": {"category_id": str(self.cat_dining_id)}}}
        )
        self.assertEqual(patch_ok.status_code, 200)

        # 3. Batch row_version changed
        prev_after_patch = self.client.get(f"/api/v1/reconciliation-batches/{batch_id}/preview", headers=self.headers)
        v_after = prev_after_patch.json()["batch"]["row_version"]
        self.assertGreater(v_after, v_before)

        # 4. Accept candidate
        accept_res = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_id}/accept",
            headers=self.headers,
            json={}
        )
        self.assertEqual(accept_res.status_code, 200)

        # 5. Commit with latest row_version succeeds
        prev_final = self.client.get(f"/api/v1/reconciliation-batches/{batch_id}/preview", headers=self.headers)
        commit_res = self.client.post(
            f"/api/v1/reconciliation-batches/{batch_id}/commit",
            headers=self.headers,
            json={"row_version": prev_final.json()["batch"]["row_version"]}
        )
        self.assertEqual(commit_res.status_code, 200)
        self.assertEqual(commit_res.json()["status"], "committed")

    def test_semantic_ambiguity_resolution_flow_and_guards(self):
        """
        Proves comprehensive semantic candidate resolution and critical direction guards:
        A. unknown debit -> TYPE_AMBIGUOUS -> cannot accept directly -> resolve as expense + category -> accept -> commit succeeds
        B. ambiguous credit -> INCOME_TRANSFER_REFUND_AMBIGUOUS -> cannot accept directly (NEVER silently becomes cash_income)
        C. choose cash_income + income category -> succeeds
        D. choose refund + valid original expense -> succeeds -> over-refund rejected
        E. choose internal transfer -> valid counter-account succeeds -> cross-household rejected -> cross-currency missing leg rejected
        """
        import app.repositories.accounts as accounts_repo
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                acc_cny_2_id = uuid4()
                accounts_repo.create_account(
                    conn=conn,
                    account_id=acc_cny_2_id,
                    household_id=self.household_id,
                    name="Second Checking CNY",
                    account_type="cash",
                    currency="CNY",
                    owner_user_id=self.user_id
                )
                acc_usd_id = uuid4()
                accounts_repo.create_account(
                    conn=conn,
                    account_id=acc_usd_id,
                    household_id=self.household_id,
                    name="USD Savings",
                    account_type="cash",
                    currency="USD",
                    owner_user_id=self.user_id
                )
                # Seed committed original expense of 200 CNY for refund testing
                orig_expense_id = uuid4()
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=orig_expense_id,
                    household_id=self.household_id,
                    transaction_type="expense",
                    occurred_on=date(2026, 8, 1),
                    original_amount=Decimal("200.00"),
                    original_currency="CNY",
                    from_amount=Decimal("200.00"),
                    from_currency="CNY",
                    from_account_id=self.acc_cny_id,
                    category_id=self.cat_dining_id,
                    merchant="Sample Restaurant",
                    reporting_amount=Decimal("200.00"),
                    reporting_currency="CNY"
                )
        finally:
            conn.close()

        # -------------------------------------------------------------
        # Scenario A: Unknown debit -> TYPE_AMBIGUOUS -> guarded accept
        # -------------------------------------------------------------
        mock_debit_extraction = StatementExtractionResult(
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            closing_balance=None,
            currency="CNY",
            lines=[
                ParsedStatementLine(
                    source_page_no=1,
                    source_row_no=1,
                    transaction_on=date(2026, 8, 5),
                    description_raw="Miscellaneous Charge 100",
                    amount=Decimal("100.00"),
                    currency="CNY",
                    direction="debit",
                    line_type="unknown"
                )
            ]
        )
        statements_router._statement_parser = MockStatementParser(result=mock_debit_extraction)
        pdf_bytes = make_pdf_bytes(["Debit Statement"])

        res_up_debit = self.client.post(
            f"/api/v1/accounts/{acc_cny_2_id}/statements",
            headers=self.headers,
            files={"file": ("stmt_debit.pdf", pdf_bytes, "application/pdf")}
        )
        self.assertEqual(res_up_debit.status_code, 201)
        batch_debit_id = res_up_debit.json()["batch_id"]

        prev_debit = self.client.get(f"/api/v1/reconciliation-batches/{batch_debit_id}/preview", headers=self.headers)
        debit_cand = prev_debit.json()["candidates"][0]
        self.assertEqual(debit_cand.get("reason_code"), "TYPE_AMBIGUOUS")

        # Scenario A1: Direct accept without resolution is strictly rejected
        acc_debit_fail = self.client.post(
            f"/api/v1/reconciliation-candidates/{debit_cand['id']}/accept",
            headers=self.headers,
            json={}
        )
        self.assertEqual(acc_debit_fail.status_code, 422)

        # Scenario A2: Resolve as expense with valid category
        res_debit_ok = self.client.post(
            f"/api/v1/reconciliation-candidates/{debit_cand['id']}/resolve",
            headers=self.headers,
            json={
                "resolution_type": "expense",
                "category_id": str(self.cat_dining_id)
            }
        )
        self.assertEqual(res_debit_ok.status_code, 200)

        # Scenario A3: Accept resolved candidate -> succeeds
        acc_debit_ok = self.client.post(
            f"/api/v1/reconciliation-candidates/{debit_cand['id']}/accept",
            headers=self.headers,
            json={}
        )
        self.assertEqual(acc_debit_ok.status_code, 200)

        # Scenario A4: Commit batch succeeds
        prev_debit_ready = self.client.get(f"/api/v1/reconciliation-batches/{batch_debit_id}/preview", headers=self.headers)
        self.assertEqual(prev_debit_ready.json()["batch"]["status"], "ready")
        commit_debit = self.client.post(
            f"/api/v1/reconciliation-batches/{batch_debit_id}/commit",
            headers=self.headers,
            json={"row_version": prev_debit_ready.json()["batch"]["row_version"]}
        )
        self.assertEqual(commit_debit.status_code, 200)
        self.assertEqual(commit_debit.json()["status"], "committed")

        # -------------------------------------------------------------
        # Scenario B & C: Ambiguous credit -> guarded accept & resolve as cash_income
        # -------------------------------------------------------------
        mock_credit_extraction = StatementExtractionResult(
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            closing_balance=None,
            currency="CNY",
            lines=[
                ParsedStatementLine(
                    source_page_no=1,
                    source_row_no=1,
                    transaction_on=date(2026, 8, 6),
                    description_raw="Deposit Inflow 600.00",
                    amount=Decimal("600.00"),
                    currency="CNY",
                    direction="credit",
                    line_type="unknown"
                )
            ]
        )
        statements_router._statement_parser = MockStatementParser(result=mock_credit_extraction)
        res_up_credit = self.client.post(
            f"/api/v1/accounts/{acc_cny_2_id}/statements",
            headers=self.headers,
            files={"file": ("stmt_credit.pdf", pdf_bytes, "application/pdf")}
        )
        self.assertEqual(res_up_credit.status_code, 201)
        batch_credit_id = res_up_credit.json()["batch_id"]

        prev_credit = self.client.get(f"/api/v1/reconciliation-batches/{batch_credit_id}/preview", headers=self.headers)
        credit_cand = prev_credit.json()["candidates"][0]
        self.assertEqual(credit_cand.get("reason_code"), "INCOME_TRANSFER_REFUND_AMBIGUOUS")

        # CRITICAL REGRESSION ASSERTION: Unresolved credit candidate must NEVER silently accept as cash_income
        acc_credit_fail = self.client.post(
            f"/api/v1/reconciliation-candidates/{credit_cand['id']}/accept",
            headers=self.headers,
            json={}
        )
        self.assertEqual(acc_credit_fail.status_code, 422)

        # Scenario C: Resolve as cash_income + income category
        res_credit_inc = self.client.post(
            f"/api/v1/reconciliation-candidates/{credit_cand['id']}/resolve",
            headers=self.headers,
            json={
                "resolution_type": "cash_income",
                "category_id": str(self.cat_income_id)
            }
        )
        self.assertEqual(res_credit_inc.status_code, 200)

        # Accept and commit cash_income
        self.assertEqual(self.client.post(f"/api/v1/reconciliation-candidates/{credit_cand['id']}/accept", headers=self.headers, json={}).status_code, 200)
        prev_c_ready = self.client.get(f"/api/v1/reconciliation-batches/{batch_credit_id}/preview", headers=self.headers)
        self.assertEqual(self.client.post(f"/api/v1/reconciliation-batches/{batch_credit_id}/commit", headers=self.headers, json={"row_version": prev_c_ready.json()["batch"]["row_version"]}).status_code, 200)

        # -------------------------------------------------------------
        # Scenario D: Resolve as refund & over-refund guard
        # -------------------------------------------------------------
        # Upload statement line with 250 CNY credit (exceeds 200 original expense)
        mock_refund_over_ext = StatementExtractionResult(
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            closing_balance=None,
            currency="CNY",
            lines=[
                ParsedStatementLine(
                    source_page_no=1,
                    source_row_no=1,
                    transaction_on=date(2026, 8, 7),
                    description_raw="Merchant Refund 250.00",
                    amount=Decimal("250.00"),
                    currency="CNY",
                    direction="credit",
                    line_type="unknown"
                )
            ]
        )
        statements_router._statement_parser = MockStatementParser(result=mock_refund_over_ext)
        res_up_ref_over = self.client.post(
            f"/api/v1/accounts/{self.acc_cny_id}/statements",
            headers=self.headers,
            files={"file": ("stmt_ref_over.pdf", pdf_bytes, "application/pdf")}
        )
        self.assertEqual(res_up_ref_over.status_code, 201)
        batch_ref_over_id = res_up_ref_over.json()["batch_id"]
        cand_ref_over = self.client.get(f"/api/v1/reconciliation-batches/{batch_ref_over_id}/preview", headers=self.headers).json()["candidates"][0]

        # Over-refund attempt: 250 > 200 -> 422 rejected!
        res_over_ref = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_ref_over['id']}/resolve",
            headers=self.headers,
            json={
                "resolution_type": "refund",
                "original_expense_id": str(orig_expense_id)
            }
        )
        self.assertEqual(res_over_ref.status_code, 422)

        # Upload valid partial refund line of 50.00 CNY
        mock_refund_valid_ext = StatementExtractionResult(
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            closing_balance=None,
            currency="CNY",
            lines=[
                ParsedStatementLine(
                    source_page_no=1,
                    source_row_no=1,
                    transaction_on=date(2026, 8, 7),
                    description_raw="Merchant Refund 50.00",
                    amount=Decimal("50.00"),
                    currency="CNY",
                    direction="credit",
                    line_type="unknown"
                )
            ]
        )
        statements_router._statement_parser = MockStatementParser(result=mock_refund_valid_ext)
        res_up_ref_valid = self.client.post(
            f"/api/v1/accounts/{self.acc_cny_id}/statements",
            headers=self.headers,
            files={"file": ("stmt_ref_valid.pdf", pdf_bytes, "application/pdf")}
        )
        self.assertEqual(res_up_ref_valid.status_code, 201)
        batch_ref_valid_id = res_up_ref_valid.json()["batch_id"]
        cand_ref_valid = self.client.get(f"/api/v1/reconciliation-batches/{batch_ref_valid_id}/preview", headers=self.headers).json()["candidates"][0]

        # Valid refund resolve succeeds
        res_ref_valid = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_ref_valid['id']}/resolve",
            headers=self.headers,
            json={
                "resolution_type": "refund",
                "original_expense_id": str(orig_expense_id)
            }
        )
        self.assertEqual(res_ref_valid.status_code, 200)
        self.assertEqual(self.client.post(f"/api/v1/reconciliation-candidates/{cand_ref_valid['id']}/accept", headers=self.headers, json={}).status_code, 200)

        # -------------------------------------------------------------
        # Scenario E: Resolve as transfer & isolation/currency guards
        # -------------------------------------------------------------
        mock_tf_ext = StatementExtractionResult(
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            closing_balance=None,
            currency="CNY",
            lines=[
                ParsedStatementLine(
                    source_page_no=1,
                    source_row_no=1,
                    transaction_on=date(2026, 8, 8),
                    description_raw="Transfer from other bank 300.00",
                    amount=Decimal("300.00"),
                    currency="CNY",
                    direction="credit",
                    line_type="unknown"
                )
            ]
        )
        statements_router._statement_parser = MockStatementParser(result=mock_tf_ext)
        res_up_tf = self.client.post(
            f"/api/v1/accounts/{self.acc_cny_id}/statements",
            headers=self.headers,
            files={"file": ("stmt_tf.pdf", pdf_bytes, "application/pdf")}
        )
        self.assertEqual(res_up_tf.status_code, 201)
        batch_tf_id = res_up_tf.json()["batch_id"]
        cand_tf = self.client.get(f"/api/v1/reconciliation-batches/{batch_tf_id}/preview", headers=self.headers).json()["candidates"][0]

        # E1. Cross-household counter-account -> 422 rejected!
        res_cross_hh = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_tf['id']}/resolve",
            headers=self.headers,
            json={
                "resolution_type": "transfer",
                "counter_account_id": str(self.acc_b_id)
            }
        )
        self.assertEqual(res_cross_hh.status_code, 422)

        # E2. Cross-currency counter-account missing explicit counter_amount -> 422 rejected!
        res_cross_curr_missing = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_tf['id']}/resolve",
            headers=self.headers,
            json={
                "resolution_type": "transfer",
                "counter_account_id": str(acc_usd_id)
            }
        )
        self.assertEqual(res_cross_curr_missing.status_code, 422)

        # E3. Same-currency valid counter-account -> succeeds!
        res_tf_valid = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_tf['id']}/resolve",
            headers=self.headers,
            json={
                "resolution_type": "transfer",
                "counter_account_id": str(acc_cny_2_id)
            }
        )
        self.assertEqual(res_tf_valid.status_code, 200)

        # Accept and commit transfer batch
        self.assertEqual(self.client.post(f"/api/v1/reconciliation-candidates/{cand_tf['id']}/accept", headers=self.headers, json={}).status_code, 200)
        prev_tf_ready = self.client.get(f"/api/v1/reconciliation-batches/{batch_tf_id}/preview", headers=self.headers)
        commit_tf = self.client.post(
            f"/api/v1/reconciliation-batches/{batch_tf_id}/commit",
            headers=self.headers,
            json={"row_version": prev_tf_ready.json()["batch"]["row_version"]}
        )
        self.assertEqual(commit_tf.status_code, 200)
        self.assertEqual(commit_tf.json()["status"], "committed")

    def test_10_semantic_resolution_direction_enforcement(self):
        """
        Integration regression test enforcing statement line direction against resolution_type:
        1. TYPE_AMBIGUOUS debit -> cash_income => 422
        2. TYPE_AMBIGUOUS debit -> refund => 422
        3. credit ambiguity -> expense => 422
        4. credit ambiguity -> fee => 422
        5. debit -> expense still succeeds
        6. credit -> cash_income still succeeds
        7. debit/credit -> valid transfer still succeeds
        8. compatible explicit match still succeeds
        """
        import app.repositories.accounts as accounts_repo
        from app.db import transaction

        # 1. Setup accounts and seed transactions
        acc_checking_id = uuid4()
        acc_counter_id = uuid4()
        orig_expense_id = uuid4()
        match_tx_id = uuid4()

        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                accounts_repo.create_account(
                    conn=conn,
                    account_id=acc_checking_id,
                    household_id=self.household_id,
                    name="Enforcement Checking",
                    account_type="cash",
                    currency="CNY",
                    owner_user_id=self.user_id
                )
                accounts_repo.create_account(
                    conn=conn,
                    account_id=acc_counter_id,
                    household_id=self.household_id,
                    name="Enforcement Counter Account",
                    account_type="cash",
                    currency="CNY",
                    owner_user_id=self.user_id
                )
                # Seed committed original expense (150.00 CNY)
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=orig_expense_id,
                    household_id=self.household_id,
                    transaction_type="expense",
                    occurred_on=date(2026, 8, 1),
                    original_amount=Decimal("150.00"),
                    original_currency="CNY",
                    from_amount=Decimal("150.00"),
                    from_currency="CNY",
                    from_account_id=acc_checking_id,
                    category_id=self.cat_dining_id,
                    merchant="Original Dining Expense",
                    reporting_amount=Decimal("150.00"),
                    reporting_currency="CNY"
                )
                # Seed committed target match transaction (80.00 CNY)
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=match_tx_id,
                    household_id=self.household_id,
                    transaction_type="expense",
                    occurred_on=date(2026, 8, 10),
                    original_amount=Decimal("80.00"),
                    original_currency="CNY",
                    from_amount=Decimal("80.00"),
                    from_currency="CNY",
                    from_account_id=acc_checking_id,
                    category_id=self.cat_dining_id,
                    merchant="Target Match Expense",
                    reporting_amount=Decimal("80.00"),
                    reporting_currency="CNY"
                )
        finally:
            conn.close()

        pdf_bytes = make_pdf_bytes(["Direction Enforcement Test"])

        # 2. Upload debit statement (TYPE_AMBIGUOUS)
        mock_debit_ext = StatementExtractionResult(
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            closing_balance=None,
            currency="CNY",
            lines=[
                ParsedStatementLine(
                    source_page_no=1,
                    source_row_no=1,
                    transaction_on=date(2026, 8, 10),
                    description_raw="Target Match Expense",
                    amount=Decimal("80.00"),
                    currency="CNY",
                    direction="debit",
                    line_type="unknown"
                ),
                ParsedStatementLine(
                    source_page_no=1,
                    source_row_no=2,
                    transaction_on=date(2026, 8, 12),
                    description_raw="Debit Transfer Out",
                    amount=Decimal("50.00"),
                    currency="CNY",
                    direction="debit",
                    line_type="unknown"
                )
            ]
        )
        statements_router._statement_parser = MockStatementParser(result=mock_debit_ext)
        res_up_debit = self.client.post(
            f"/api/v1/accounts/{acc_checking_id}/statements",
            headers=self.headers,
            files={"file": ("debit_stmt.pdf", pdf_bytes, "application/pdf")}
        )
        self.assertEqual(res_up_debit.status_code, 201)
        batch_debit_id = res_up_debit.json()["batch_id"]

        prev_debit = self.client.get(f"/api/v1/reconciliation-batches/{batch_debit_id}/preview", headers=self.headers)
        debit_cands = prev_debit.json()["candidates"]
        cand_debit_1 = debit_cands[0]
        cand_debit_2 = debit_cands[1]
        self.assertEqual(cand_debit_1.get("reason_code"), "TYPE_AMBIGUOUS")

        # 3. Upload credit statement (INCOME_TRANSFER_REFUND_AMBIGUOUS)
        mock_credit_ext = StatementExtractionResult(
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            closing_balance=None,
            currency="CNY",
            lines=[
                ParsedStatementLine(
                    source_page_no=1,
                    source_row_no=1,
                    transaction_on=date(2026, 8, 15),
                    description_raw="Credit Ambiguous Inflow",
                    amount=Decimal("120.00"),
                    currency="CNY",
                    direction="credit",
                    line_type="unknown"
                ),
                ParsedStatementLine(
                    source_page_no=1,
                    source_row_no=2,
                    transaction_on=date(2026, 8, 16),
                    description_raw="Credit Transfer In",
                    amount=Decimal("70.00"),
                    currency="CNY",
                    direction="credit",
                    line_type="unknown"
                )
            ]
        )
        statements_router._statement_parser = MockStatementParser(result=mock_credit_ext)
        res_up_credit = self.client.post(
            f"/api/v1/accounts/{acc_checking_id}/statements",
            headers=self.headers,
            files={"file": ("credit_stmt.pdf", pdf_bytes, "application/pdf")}
        )
        self.assertEqual(res_up_credit.status_code, 201)
        batch_credit_id = res_up_credit.json()["batch_id"]

        prev_credit = self.client.get(f"/api/v1/reconciliation-batches/{batch_credit_id}/preview", headers=self.headers)
        credit_cands = prev_credit.json()["candidates"]
        cand_credit_1 = credit_cands[0]
        cand_credit_2 = credit_cands[1]
        self.assertEqual(cand_credit_1.get("reason_code"), "INCOME_TRANSFER_REFUND_AMBIGUOUS")

        # 1. TYPE_AMBIGUOUS debit -> cash_income => 422
        res_1 = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_debit_1['id']}/resolve",
            headers=self.headers,
            json={"resolution_type": "cash_income", "category_id": str(self.cat_income_id)}
        )
        self.assertEqual(res_1.status_code, 422)

        # 2. TYPE_AMBIGUOUS debit -> refund => 422
        res_2 = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_debit_1['id']}/resolve",
            headers=self.headers,
            json={"resolution_type": "refund", "original_expense_id": str(orig_expense_id)}
        )
        self.assertEqual(res_2.status_code, 422)

        # 3. credit ambiguity -> expense => 422
        res_3 = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_credit_1['id']}/resolve",
            headers=self.headers,
            json={"resolution_type": "expense", "category_id": str(self.cat_dining_id)}
        )
        self.assertEqual(res_3.status_code, 422)

        # 4. credit ambiguity -> fee => 422
        res_4 = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_credit_1['id']}/resolve",
            headers=self.headers,
            json={"resolution_type": "fee", "category_id": str(self.cat_dining_id)}
        )
        self.assertEqual(res_4.status_code, 422)

        # 5. debit -> expense still succeeds
        res_5 = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_debit_1['id']}/resolve",
            headers=self.headers,
            json={"resolution_type": "expense", "category_id": str(self.cat_dining_id)}
        )
        self.assertEqual(res_5.status_code, 200)

        # 6. credit -> cash_income still succeeds
        res_6 = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_credit_1['id']}/resolve",
            headers=self.headers,
            json={"resolution_type": "cash_income", "category_id": str(self.cat_income_id)}
        )
        self.assertEqual(res_6.status_code, 200)

        # 7. debit/credit -> valid transfer still succeeds
        # Debit line transfer out (from checking to counter account)
        res_7_debit = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_debit_2['id']}/resolve",
            headers=self.headers,
            json={"resolution_type": "transfer", "counter_account_id": str(acc_counter_id)}
        )
        self.assertEqual(res_7_debit.status_code, 200)

        # Credit line transfer in (from counter account to checking)
        res_7_credit = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_credit_2['id']}/resolve",
            headers=self.headers,
            json={"resolution_type": "transfer", "counter_account_id": str(acc_counter_id)}
        )
        self.assertEqual(res_7_credit.status_code, 200)

        # 8. compatible explicit match still succeeds
        res_8 = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_debit_1['id']}/resolve",
            headers=self.headers,
            json={"resolution_type": "match", "target_transaction_id": str(match_tx_id)}
        )
        self.assertEqual(res_8.status_code, 200)

    def test_11_candidate_mutation_bypass_regression(self):
        """
        Comprehensive regression tests for bypass paths across all Backend mutation endpoints:
        A. debit TYPE_AMBIGUOUS -> generic PATCH transaction_type=cash_income + income category => 422 => reason remains unresolved => cannot accept
        B. credit INCOME_TRANSFER_REFUND_AMBIGUOUS -> generic PATCH transaction_type=expense + expense category => 422
        C. debit create_transfer PATCH with reconciled account on to_account side => 422
        D. credit create_transfer PATCH with reconciled account on from_account side => 422
        E. MULTIPLE_TRANSACTION_MATCHES -> /resolve expense => 422
        F. CATEGORY_REQUIRED -> /resolve cash_income/expense semantic rewrite => 422 (normal category PATCH must still succeed)
        G. accepted candidate -> /resolve again => 422
        H. valid TYPE_AMBIGUOUS debit -> /resolve expense => still succeeds
        I. valid credit ambiguity -> /resolve cash_income/refund/transfer/match => still succeeds
        J. existing CATEGORY_REQUIRED safe category PATCH => still succeeds
        """
        import app.repositories.accounts as accounts_repo
        from app.db import transaction

        # 1. Setup accounts and seed transactions
        acc_checking_id = uuid4()
        acc_counter_id = uuid4()
        orig_expense_id = uuid4()
        match_tx_1 = uuid4()
        match_tx_2 = uuid4()

        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                accounts_repo.create_account(
                    conn=conn,
                    account_id=acc_checking_id,
                    household_id=self.household_id,
                    name="Bypass Test Checking",
                    account_type="cash",
                    currency="CNY",
                    owner_user_id=self.user_id
                )
                accounts_repo.create_account(
                    conn=conn,
                    account_id=acc_counter_id,
                    household_id=self.household_id,
                    name="Bypass Test Counter",
                    account_type="cash",
                    currency="CNY",
                    owner_user_id=self.user_id
                )
                # Seed original expense for refund test
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=orig_expense_id,
                    household_id=self.household_id,
                    transaction_type="expense",
                    occurred_on=date(2026, 8, 1),
                    original_amount=Decimal("200.00"),
                    original_currency="CNY",
                    from_amount=Decimal("200.00"),
                    from_currency="CNY",
                    from_account_id=acc_checking_id,
                    category_id=self.cat_dining_id,
                    merchant="Original Dining",
                    reporting_amount=Decimal("200.00"),
                    reporting_currency="CNY"
                )
                # Seed 2 transactions for MULTIPLE_TRANSACTION_MATCHES
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=match_tx_1,
                    household_id=self.household_id,
                    transaction_type="expense",
                    occurred_on=date(2026, 8, 20),
                    original_amount=Decimal("30.00"),
                    original_currency="CNY",
                    from_amount=Decimal("30.00"),
                    from_currency="CNY",
                    from_account_id=acc_checking_id,
                    category_id=self.cat_dining_id,
                    merchant="Coffee Shop A",
                    reporting_amount=Decimal("30.00"),
                    reporting_currency="CNY"
                )
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=match_tx_2,
                    household_id=self.household_id,
                    transaction_type="expense",
                    occurred_on=date(2026, 8, 20),
                    original_amount=Decimal("30.00"),
                    original_currency="CNY",
                    from_amount=Decimal("30.00"),
                    from_currency="CNY",
                    from_account_id=acc_checking_id,
                    category_id=self.cat_dining_id,
                    merchant="Coffee Shop B",
                    reporting_amount=Decimal("30.00"),
                    reporting_currency="CNY"
                )
        finally:
            conn.close()

        pdf_bytes = make_pdf_bytes(["Bypass Regression Test"])

        # Upload debit statement:
        # Row 1: TYPE_AMBIGUOUS (debit)
        # Row 2: MULTIPLE_TRANSACTION_MATCHES (30.00 CNY)
        # Row 3: CATEGORY_REQUIRED (debit expense without default category)
        mock_debit_ext = StatementExtractionResult(
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            closing_balance=None,
            currency="CNY",
            lines=[
                ParsedStatementLine(
                    source_page_no=1,
                    source_row_no=1,
                    transaction_on=date(2026, 8, 5),
                    description_raw="Ambiguous Debit Line",
                    amount=Decimal("55.00"),
                    currency="CNY",
                    direction="debit",
                    line_type="unknown"
                ),
                ParsedStatementLine(
                    source_page_no=1,
                    source_row_no=2,
                    transaction_on=date(2026, 8, 20),
                    description_raw="Coffee Shop",
                    amount=Decimal("30.00"),
                    currency="CNY",
                    direction="debit",
                    line_type="expense"
                ),
                ParsedStatementLine(
                    source_page_no=1,
                    source_row_no=3,
                    transaction_on=date(2026, 8, 25),
                    description_raw="Supermarket Grocery",
                    amount=Decimal("45.00"),
                    currency="CNY",
                    direction="debit",
                    line_type="expense"
                )
            ]
        )
        statements_router._statement_parser = MockStatementParser(result=mock_debit_ext)
        # Upload without default expense category
        res_up_debit = self.client.post(
            f"/api/v1/accounts/{acc_checking_id}/statements",
            headers=self.headers,
            files={"file": ("debit_stmt.pdf", pdf_bytes, "application/pdf")}
        )
        self.assertEqual(res_up_debit.status_code, 201)
        batch_debit_id = res_up_debit.json()["batch_id"]

        prev_debit = self.client.get(f"/api/v1/reconciliation-batches/{batch_debit_id}/preview", headers=self.headers)
        debit_cands = prev_debit.json()["candidates"]
        cand_debit_ambig = next(c for c in debit_cands if c.get("reason_code") == "TYPE_AMBIGUOUS")
        cand_debit_multi = next(c for c in debit_cands if c.get("reason_code") == "MULTIPLE_TRANSACTION_MATCHES")
        cand_debit_cat_req = next(c for c in debit_cands if c.get("reason_code") == "CATEGORY_REQUIRED")

        # Upload credit statement:
        # Row 1: INCOME_TRANSFER_REFUND_AMBIGUOUS (credit)
        mock_credit_ext = StatementExtractionResult(
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            closing_balance=None,
            currency="CNY",
            lines=[
                ParsedStatementLine(
                    source_page_no=1,
                    source_row_no=1,
                    transaction_on=date(2026, 8, 15),
                    description_raw="Ambiguous Inflow Line",
                    amount=Decimal("100.00"),
                    currency="CNY",
                    direction="credit",
                    line_type="unknown"
                )
            ]
        )
        statements_router._statement_parser = MockStatementParser(result=mock_credit_ext)
        res_up_credit = self.client.post(
            f"/api/v1/accounts/{acc_checking_id}/statements",
            headers=self.headers,
            files={"file": ("credit_stmt.pdf", pdf_bytes, "application/pdf")}
        )
        self.assertEqual(res_up_credit.status_code, 201)
        batch_credit_id = res_up_credit.json()["batch_id"]

        prev_credit = self.client.get(f"/api/v1/reconciliation-batches/{batch_credit_id}/preview", headers=self.headers)
        credit_cands = prev_credit.json()["candidates"]
        cand_credit_ambig = credit_cands[0]
        self.assertEqual(cand_credit_ambig.get("reason_code"), "INCOME_TRANSFER_REFUND_AMBIGUOUS")

        # A. debit TYPE_AMBIGUOUS -> generic PATCH transaction_type=cash_income + income category => 422
        res_a_patch = self.client.patch(
            f"/api/v1/reconciliation-candidates/{cand_debit_ambig['id']}",
            headers=self.headers,
            json={
                "payload": {
                    "transaction": {
                        "transaction_type": "cash_income",
                        "category_id": str(self.cat_income_id)
                    }
                }
            }
        )
        self.assertEqual(res_a_patch.status_code, 422)

        # Reason remains unresolved and cannot be accepted
        res_a_accept = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_debit_ambig['id']}/accept",
            headers=self.headers,
            json={}
        )
        self.assertEqual(res_a_accept.status_code, 422)

        # B. credit INCOME_TRANSFER_REFUND_AMBIGUOUS -> generic PATCH transaction_type=expense + expense category => 422
        res_b_patch = self.client.patch(
            f"/api/v1/reconciliation-candidates/{cand_credit_ambig['id']}",
            headers=self.headers,
            json={
                "payload": {
                    "transaction": {
                        "transaction_type": "expense",
                        "category_id": str(self.cat_dining_id)
                    }
                }
            }
        )
        self.assertEqual(res_b_patch.status_code, 422)

        # C. debit create_transfer PATCH with reconciled account on to_account side => 422
        # First resolve as transfer
        self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_debit_ambig['id']}/resolve",
            headers=self.headers,
            json={"resolution_type": "transfer", "counter_account_id": str(acc_counter_id)}
        )
        # Attempt PATCH that puts reconciled account on to_account_id for a debit line
        res_c = self.client.patch(
            f"/api/v1/reconciliation-candidates/{cand_debit_ambig['id']}",
            headers=self.headers,
            json={
                "payload": {
                    "transfer": {
                        "from_account_id": str(acc_counter_id),
                        "to_account_id": str(acc_checking_id)
                    }
                }
            }
        )
        self.assertEqual(res_c.status_code, 422)

        # D. credit create_transfer PATCH with reconciled account on from_account side => 422
        # First resolve as transfer
        self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_credit_ambig['id']}/resolve",
            headers=self.headers,
            json={"resolution_type": "transfer", "counter_account_id": str(acc_counter_id)}
        )
        # Attempt PATCH that puts reconciled account on from_account_id for a credit line
        res_d = self.client.patch(
            f"/api/v1/reconciliation-candidates/{cand_credit_ambig['id']}",
            headers=self.headers,
            json={
                "payload": {
                    "transfer": {
                        "from_account_id": str(acc_checking_id),
                        "to_account_id": str(acc_counter_id)
                    }
                }
            }
        )
        self.assertEqual(res_d.status_code, 422)

        # E. MULTIPLE_TRANSACTION_MATCHES -> /resolve expense => 422
        res_e = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_debit_multi['id']}/resolve",
            headers=self.headers,
            json={"resolution_type": "expense", "category_id": str(self.cat_dining_id)}
        )
        self.assertEqual(res_e.status_code, 422)

        # F. CATEGORY_REQUIRED -> /resolve cash_income/expense semantic rewrite => 422
        res_f_rewrite = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_debit_cat_req['id']}/resolve",
            headers=self.headers,
            json={"resolution_type": "cash_income", "category_id": str(self.cat_income_id)}
        )
        self.assertEqual(res_f_rewrite.status_code, 422)

        # G. accepted candidate -> /resolve again => 422
        # Accept the multi candidate with explicit target match
        res_accept_multi = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_debit_multi['id']}/accept",
            headers=self.headers,
            json={"target_transaction_id": str(match_tx_1)}
        )
        self.assertEqual(res_accept_multi.status_code, 200)
        # Attempting /resolve on accepted candidate must fail with 422
        res_g = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_debit_multi['id']}/resolve",
            headers=self.headers,
            json={"resolution_type": "expense", "category_id": str(self.cat_dining_id)}
        )
        self.assertEqual(res_g.status_code, 422)

        # H. valid TYPE_AMBIGUOUS debit -> /resolve expense => still succeeds
        res_h = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_debit_ambig['id']}/resolve",
            headers=self.headers,
            json={"resolution_type": "expense", "category_id": str(self.cat_dining_id)}
        )
        self.assertEqual(res_h.status_code, 200)

        # I. valid credit ambiguity -> /resolve cash_income/refund/transfer/match => still succeeds
        res_i = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_credit_ambig['id']}/resolve",
            headers=self.headers,
            json={"resolution_type": "cash_income", "category_id": str(self.cat_income_id)}
        )
        self.assertEqual(res_i.status_code, 200)

        # J. existing CATEGORY_REQUIRED safe category PATCH => still succeeds
        res_j_patch = self.client.patch(
            f"/api/v1/reconciliation-candidates/{cand_debit_cat_req['id']}",
            headers=self.headers,
            json={"payload": {"transaction": {"category_id": str(self.cat_dining_id)}}}
        )
        self.assertEqual(res_j_patch.status_code, 200)
        # Now CATEGORY_REQUIRED is cleared, candidate can be accepted
        res_j_accept = self.client.post(
            f"/api/v1/reconciliation-candidates/{cand_debit_cat_req['id']}/accept",
            headers=self.headers,
            json={}
        )
        self.assertEqual(res_j_accept.status_code, 200)


if __name__ == "__main__":
    unittest.main()
