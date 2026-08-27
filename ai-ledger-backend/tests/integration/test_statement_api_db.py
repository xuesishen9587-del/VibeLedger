import os
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test_user:test_pass@127.0.0.1:5432/vibeledger_test")
os.environ.setdefault("DB_SCHEMA", "vibeledger_test_runner")

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


if __name__ == "__main__":
    unittest.main()
