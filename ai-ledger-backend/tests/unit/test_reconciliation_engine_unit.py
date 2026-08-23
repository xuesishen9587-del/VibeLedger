import unittest
from datetime import date
from decimal import Decimal
from uuid import uuid4

from app.domain.reconciliation.models import (
    NormalizedStatementLine,
    CandidateProposal,
    ReconciliationResult,
    MatchScore,
    AUTO_MATCH_SCORE,
    AUTO_MATCH_MARGIN,
    AUTO_ADJUST_THRESHOLD_CNY,
    NO_MATCH,
    LOW_MATCH_SCORE,
    MULTIPLE_TRANSACTION_MATCHES,
    AMOUNT_CONFLICT,
    ORIGINAL_AMOUNT_CONFLICT,
    DATE_OUTSIDE_WINDOW,
    SETTLEMENT_DEVIATION_SUSPICIOUS,
    COUNTER_ACCOUNT_UNRESOLVED,
    CROSS_CURRENCY_MISSING_LEG,
    REFUND_ORIGINAL_NOT_FOUND,
    MULTIPLE_REFUND_ORIGINALS,
    REFUND_EXCEEDS_ORIGINAL,
    INSTALLMENT_PLAN_AMBIGUOUS,
    RECONCILIATION_RESIDUAL_TOO_LARGE,
    INCOME_TRANSFER_REFUND_AMBIGUOUS,
    TYPE_AMBIGUOUS
)
from app.domain.reconciliation.normalizer import normalize_description
from app.domain.reconciliation.scoring import compute_match_score, trigram_similarity
from app.domain.reconciliation.matcher import match_statement_lines_to_transactions
from app.domain.reconciliation.transfers import process_transfer_line
from app.domain.reconciliation.refunds import process_refund_line
from app.domain.reconciliation.installments import process_installment_line
from app.domain.reconciliation.residuals import evaluate_residual_and_batch_readiness, simulate_candidate_effects
from app.domain.reconciliation.engine import run_deterministic_reconciliation
from app.domain.money import quantize_money, parse_decimal


class TestReconciliationEngineUnit(unittest.TestCase):

    def setUp(self):
        self.account_id = uuid4()
        self.counter_account_id = uuid4()
        self.usd_account_id = uuid4()

    # =========================================================================
    # 1. NORMALIZATION TESTS (Section 7 & Section 42)
    # =========================================================================

    def test_01_description_nfkc_and_whitespace_normalization(self):
        # Unicode NFKC + Latin lowercase + whitespace collapse
        self.assertEqual(normalize_description("APPLE.COM/BILL  BEIJING"), "apple com bill beijing")
        self.assertEqual(normalize_description("支付宝-盒马鲜生"), "支付宝 盒马鲜生")
        self.assertEqual(normalize_description("STARBUCKS   001"), "starbucks 001")
        self.assertEqual(normalize_description("STARBUCKS   002"), "starbucks 002")
        self.assertEqual(normalize_description("  全角文字：１２３４５　ＡＢＣ  "), "全角文字 12345 abc")

    # =========================================================================
    # 2. SCORING COMPONENTS TESTS (Section 15, 16, 17 & Section 42)
    # =========================================================================

    def test_02_date_scores_exact_matrix(self):
        base_line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Test",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("100.00"),
            settlement_currency="CNY"
        )
        for days_diff, expected_score in [(0, 20), (1, 18), (2, 16), (3, 12), (4, 8), (5, 5)]:
            tx = {
                "id": uuid4(),
                "occurred_on": date(2026, 8, 10 + days_diff),
                "from_account_id": self.account_id,
                "from_amount": Decimal("100.00"),
                "from_currency": "CNY",
                "transaction_type": "expense",
                "account_leg_status": "authoritative"
            }
            score = compute_match_score(base_line, tx, self.account_id)
            self.assertEqual(score.date_score, expected_score, f"Failed for {days_diff} days difference")
            self.assertFalse(score.is_blocked)

    def test_03_date_outside_window_fails_gate(self):
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Test",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("100.00"),
            settlement_currency="CNY"
        )
        tx = {
            "id": uuid4(),
            "occurred_on": date(2026, 8, 16),  # +6 days
            "from_account_id": self.account_id,
            "from_amount": Decimal("100.00"),
            "from_currency": "CNY",
            "transaction_type": "expense"
        }
        score = compute_match_score(line, tx, self.account_id)
        self.assertTrue(score.is_blocked)
        self.assertEqual(score.block_reason, DATE_OUTSIDE_WINDOW)

    def test_04_merchant_similarity_threshold_matrix(self):
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="STARBUCKS COFFEE BEIJING",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("35.00"),
            settlement_currency="CNY"
        )
        # Exact (score 20)
        tx_exact = {"id": uuid4(), "occurred_on": date(2026, 8, 10), "from_account_id": self.account_id,
                    "from_amount": Decimal("35.00"), "from_currency": "CNY", "merchant": "STARBUCKS COFFEE BEIJING", "transaction_type": "expense"}
        s_exact = compute_match_score(line, tx_exact, self.account_id)
        self.assertEqual(s_exact.merchant_score, 20)

        # Absent merchant (score 0)
        tx_none = {"id": uuid4(), "occurred_on": date(2026, 8, 10), "from_account_id": self.account_id,
                   "from_amount": Decimal("35.00"), "from_currency": "CNY", "transaction_type": "expense"}
        s_none = compute_match_score(line, tx_none, self.account_id)
        self.assertEqual(s_none.merchant_score, 0)

    def test_05_type_compatibility_matrix(self):
        for line_t, tx_t, expected in [
            ("expense", "expense", 10),
            ("fee", "fee", 10),
            ("refund", "refund", 10),
            ("transfer", "transfer", 10),
            ("unknown", "expense", 0),
            ("expense", "income", 0)
        ]:
            line = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Test",
                direction="debit",
                line_type=line_t,
                settlement_amount=Decimal("100.00"),
                settlement_currency="CNY"
            )
            tx = {
                "id": uuid4(),
                "occurred_on": date(2026, 8, 10),
                "from_account_id": self.account_id,
                "from_amount": Decimal("100.00"),
                "from_currency": "CNY",
                "transaction_type": tx_t
            }
            score = compute_match_score(line, tx, self.account_id)
            self.assertEqual(score.type_score, expected, f"Failed for line {line_t} vs tx {tx_t}")

    # =========================================================================
    # 3. AMOUNT EVIDENCE & CONTRADICTION TESTS (Section 11, 14 & Section 42)
    # =========================================================================

    def test_06_authoritative_amount_conflict_blocked(self):
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Starbucks",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("35.00"),
            settlement_currency="CNY"
        )
        tx = {
            "id": uuid4(),
            "occurred_on": date(2026, 8, 10),
            "from_account_id": self.account_id,
            "from_amount": Decimal("38.00"),  # Contradicts 35.00
            "from_currency": "CNY",
            "merchant": "Starbucks",
            "transaction_type": "expense",
            "account_leg_status": "authoritative"
        }
        score = compute_match_score(line, tx, self.account_id)
        self.assertTrue(score.is_blocked)
        self.assertEqual(score.block_reason, AMOUNT_CONFLICT)

    def test_07_foreign_card_estimated_settlement_scoring(self):
        # Shortcut captured 10000 JPY -> estimated 68.90 USD. Statement settles at 68.20 USD (1.0% dev)
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 12),
            description_raw="Tokyo Shop",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("68.20"),
            settlement_currency="USD"
        )
        tx = {
            "id": uuid4(),
            "occurred_on": date(2026, 8, 10),
            "from_account_id": self.account_id,
            "from_amount": Decimal("68.90"),
            "from_currency": "USD",
            "original_amount": Decimal("10000"),
            "original_currency": "JPY",
            "merchant": "Tokyo Shop",
            "transaction_type": "expense",
            "account_leg_status": "estimated"
        }
        score = compute_match_score(line, tx, self.account_id)
        self.assertFalse(score.is_blocked)
        self.assertEqual(score.amount_score, 35)
        self.assertEqual(score.date_score, 16)  # 2 days diff
        self.assertEqual(score.merchant_score, 20)
        self.assertEqual(score.type_score, 10)
        self.assertEqual(score.total_score, 81)  # 35 + 16 + 20 + 10 = 81 >= 80

    def test_08_foreign_card_estimated_settlement_suspicious_deviation(self):
        # Estimated 68.90 USD vs Statement 100.00 USD (>20% dev)
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Tokyo Shop",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("100.00"),
            settlement_currency="USD"
        )
        tx = {
            "id": uuid4(),
            "occurred_on": date(2026, 8, 10),
            "from_account_id": self.account_id,
            "from_amount": Decimal("68.90"),
            "from_currency": "USD",
            "merchant": "Tokyo Shop",
            "transaction_type": "expense",
            "account_leg_status": "estimated"
        }
        score = compute_match_score(line, tx, self.account_id)
        self.assertTrue(score.is_blocked)
        self.assertEqual(score.block_reason, SETTLEMENT_DEVIATION_SUSPICIOUS)

    def test_09_original_amount_conflict_blocked(self):
        # Captured 10000 JPY vs Statement 12000 JPY
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Tokyo Shop",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("68.20"),
            settlement_currency="USD",
            original_amount=Decimal("12000"),
            original_currency="JPY"
        )
        tx = {
            "id": uuid4(),
            "occurred_on": date(2026, 8, 10),
            "from_account_id": self.account_id,
            "from_amount": Decimal("68.90"),
            "from_currency": "USD",
            "original_amount": Decimal("10000"),
            "original_currency": "JPY",
            "merchant": "Tokyo Shop",
            "transaction_type": "expense",
            "account_leg_status": "estimated"
        }
        score = compute_match_score(line, tx, self.account_id)
        self.assertTrue(score.is_blocked)
        self.assertEqual(score.block_reason, ORIGINAL_AMOUNT_CONFLICT)

    # =========================================================================
    # 4. MATCHER & MUTUAL-BEST TESTS (Section 12, 18 & Section 42)
    # =========================================================================

    def test_10_exact_ordinary_match_auto_accepted(self):
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Starbucks",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("35.00"),
            settlement_currency="CNY"
        )
        tx = {
            "id": uuid4(),
            "occurred_on": date(2026, 8, 10),
            "from_account_id": self.account_id,
            "from_amount": Decimal("35.00"),
            "from_currency": "CNY",
            "merchant": "Starbucks",
            "transaction_type": "expense",
            "status": "committed"
        }
        candidates, unmatched = match_statement_lines_to_transactions([line], [tx], self.account_id)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].status, "accepted")
        self.assertEqual(candidates[0].target_transaction_id, tx["id"])
        self.assertEqual(len(unmatched), 0)

    def test_11_single_candidate_score_under_80_needs_review(self):
        # Match with weak merchant similarity (score = 40 + 20 + 0 + 10 = 70 < 80)
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Unknown Merchant XYZ",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("35.00"),
            settlement_currency="CNY"
        )
        tx = {
            "id": uuid4(),
            "occurred_on": date(2026, 8, 10),
            "from_account_id": self.account_id,
            "from_amount": Decimal("35.00"),
            "from_currency": "CNY",
            "merchant": "Starbucks",
            "transaction_type": "expense",
            "status": "committed"
        }
        candidates, unmatched = match_statement_lines_to_transactions([line], [tx], self.account_id)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].status, "needs_review")
        self.assertEqual(candidates[0].reason_code, LOW_MATCH_SCORE)

    def test_12_multiple_candidate_margin_under_15_needs_review(self):
        # Two Starbucks transactions on same day with same amount -> margin = 0 < 15
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Starbucks",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("35.00"),
            settlement_currency="CNY"
        )
        tx1 = {"id": uuid4(), "occurred_on": date(2026, 8, 10), "from_account_id": self.account_id,
               "from_amount": Decimal("35.00"), "from_currency": "CNY", "merchant": "Starbucks", "transaction_type": "expense", "status": "committed"}
        tx2 = {"id": uuid4(), "occurred_on": date(2026, 8, 10), "from_account_id": self.account_id,
               "from_amount": Decimal("35.00"), "from_currency": "CNY", "merchant": "Starbucks", "transaction_type": "expense", "status": "committed"}

        candidates, unmatched = match_statement_lines_to_transactions([line], [tx1, tx2], self.account_id)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].status, "needs_review")
        self.assertEqual(candidates[0].reason_code, MULTIPLE_TRANSACTION_MATCHES)

    def test_13_mutual_best_conflict_no_double_assignment(self):
        # Two statement lines compete for the exact same single transaction
        line1 = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Starbucks",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("35.00"),
            settlement_currency="CNY"
        )
        line2 = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Starbucks",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("35.00"),
            settlement_currency="CNY"
        )
        tx = {
            "id": uuid4(),
            "occurred_on": date(2026, 8, 10),
            "from_account_id": self.account_id,
            "from_amount": Decimal("35.00"),
            "from_currency": "CNY",
            "merchant": "Starbucks",
            "transaction_type": "expense",
            "status": "committed"
        }
        candidates, _ = match_statement_lines_to_transactions([line1, line2], [tx], self.account_id)
        self.assertEqual(len(candidates), 2)
        # Neither auto-accepted
        for c in candidates:
            self.assertEqual(c.status, "needs_review")
            self.assertEqual(c.reason_code, MULTIPLE_TRANSACTION_MATCHES)

    def test_14_unknown_direction_restricted_to_review(self):
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Starbucks",
            direction="unknown",
            line_type="unknown",
            settlement_amount=Decimal("35.00"),
            settlement_currency="CNY"
        )
        tx = {
            "id": uuid4(),
            "occurred_on": date(2026, 8, 10),
            "from_account_id": self.account_id,
            "from_amount": Decimal("35.00"),
            "from_currency": "CNY",
            "merchant": "Starbucks",
            "transaction_type": "expense",
            "status": "committed"
        }
        candidates, _ = match_statement_lines_to_transactions([line], [tx], self.account_id)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].status, "needs_review")
        self.assertEqual(candidates[0].reason_code, TYPE_AMBIGUOUS)

    # =========================================================================
    # 5. TRANSFER TESTS (Section 19, 20 & Section 43)
    # =========================================================================

    # =========================================================================
    # 5. TRANSFER TESTS (Section 19, 20 & Section 43)
    # =========================================================================

    def test_15_transfer_same_currency_two_leg_accepted(self):
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="转账",
            direction="debit",
            line_type="transfer",
            settlement_amount=Decimal("5000.00"),
            settlement_currency="CNY"
        )
        hh_movements = [{
            "account_id": self.counter_account_id,
            "direction": "credit",
            "amount": Decimal("5000.00"),
            "currency": "CNY",
            "occurred_on": date(2026, 8, 10),
            "is_counter_statement_leg": True
        }]
        cand = process_transfer_line(line, self.account_id, [], hh_movements)
        self.assertEqual(cand.status, "accepted")
        self.assertEqual(cand.candidate_type, "create_transfer")
        t_data = cand.payload["transfer"]
        self.assertEqual(t_data["from_amount"], "5000.00")
        self.assertEqual(t_data["to_amount"], "5000.00")
        self.assertEqual(t_data["effective_fx_rate"], "1.000000")

    def test_16_transfer_cross_currency_two_leg_accepted(self):
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Cross Currency Transfer",
            direction="debit",
            line_type="transfer",
            settlement_amount=Decimal("7250.00"),
            settlement_currency="CNY"
        )
        hh_movements = [{
            "account_id": self.usd_account_id,
            "direction": "credit",
            "amount": Decimal("1000.00"),
            "currency": "USD",
            "occurred_on": date(2026, 8, 10),
            "is_counter_statement_leg": True
        }]
        cand = process_transfer_line(line, self.account_id, [], hh_movements)
        self.assertEqual(cand.status, "accepted")
        t_data = cand.payload["transfer"]
        self.assertEqual(t_data["from_amount"], "7250.00")
        self.assertEqual(t_data["to_amount"], "1000.00")
        self.assertEqual(t_data["effective_fx_rate"], "7.250000000000")

    def test_17_transfer_cross_currency_missing_leg_needs_review(self):
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Transfer to USD Account",
            direction="debit",
            line_type="transfer",
            settlement_amount=Decimal("7250.00"),
            settlement_currency="CNY",
            original_amount=Decimal("1000.00"),
            original_currency="USD"
        )
        cand = process_transfer_line(line, self.account_id, [], [])
        self.assertEqual(cand.status, "needs_review")
        self.assertEqual(cand.reason_code, CROSS_CURRENCY_MISSING_LEG)

    def test_18_transfer_counter_account_ambiguity_needs_review(self):
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="转账",
            direction="debit",
            line_type="transfer",
            settlement_amount=Decimal("5000.00"),
            settlement_currency="CNY"
        )
        acc_b = uuid4()
        acc_c = uuid4()
        hh_movements = [
            {"account_id": acc_b, "direction": "credit", "amount": Decimal("5000.00"), "currency": "CNY", "occurred_on": date(2026, 8, 10), "is_counter_statement_leg": True},
            {"account_id": acc_c, "direction": "credit", "amount": Decimal("5000.00"), "currency": "CNY", "occurred_on": date(2026, 8, 10), "is_counter_statement_leg": True}
        ]
        cand = process_transfer_line(line, self.account_id, [], hh_movements)
        self.assertEqual(cand.status, "needs_review")
        self.assertEqual(cand.reason_code, COUNTER_ACCOUNT_UNRESOLVED)

    # =========================================================================
    # 6. REFUND TESTS (Section 21 & Section 44)
    # =========================================================================

    def test_19_refund_exact_original_accepted(self):
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Apple Store",
            merchant_hint="Apple Store",
            direction="credit",
            line_type="refund",
            settlement_amount=Decimal("300.00"),
            settlement_currency="CNY"
        )
        orig_exp_id = uuid4()
        candidate_expenses = [{
            "id": orig_exp_id,
            "occurred_on": date(2026, 7, 15),
            "from_account_id": self.account_id,
            "from_amount": Decimal("1000.00"),
            "from_currency": "CNY",
            "merchant": "Apple Store",
            "transaction_type": "expense",
            "status": "committed"
        }]
        cand = process_refund_line(line, self.account_id, candidate_expenses, {})
        self.assertEqual(cand.status, "accepted")
        self.assertEqual(cand.candidate_type, "refund")
        self.assertEqual(cand.payload["refund"]["original_expense_id"], str(orig_exp_id))
        self.assertEqual(cand.payload["refund"]["amount"], "300.00")

    def test_20_refund_exceeds_remaining_refundable_needs_review(self):
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Apple Store",
            direction="credit",
            line_type="refund",
            settlement_amount=Decimal("300.00"),
            settlement_currency="CNY"
        )
        orig_exp_id = uuid4()
        candidate_expenses = [{
            "id": orig_exp_id,
            "occurred_on": date(2026, 7, 15),
            "from_account_id": self.account_id,
            "from_amount": Decimal("1000.00"),
            "from_currency": "CNY",
            "merchant": "Apple Store",
            "transaction_type": "expense",
            "status": "committed"
        }]
        existing_totals = {orig_exp_id: Decimal("800.00")}
        cand = process_refund_line(line, self.account_id, candidate_expenses, existing_totals)
        self.assertEqual(cand.status, "needs_review")
        self.assertEqual(cand.reason_code, REFUND_EXCEEDS_ORIGINAL)

    def test_21_refund_outside_180_days_needs_review(self):
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Apple Store",
            direction="credit",
            line_type="refund",
            settlement_amount=Decimal("300.00"),
            settlement_currency="CNY"
        )
        orig_exp_id = uuid4()
        candidate_expenses = [{
            "id": orig_exp_id,
            "occurred_on": date(2026, 1, 1),  # > 180 days ago
            "from_account_id": self.account_id,
            "from_amount": Decimal("1000.00"),
            "from_currency": "CNY",
            "merchant": "Apple Store",
            "transaction_type": "expense",
            "status": "committed"
        }]
        cand = process_refund_line(line, self.account_id, candidate_expenses, {})
        self.assertEqual(cand.status, "needs_review")
        self.assertEqual(cand.reason_code, REFUND_ORIGINAL_NOT_FOUND)

    # =========================================================================
    # 7. INSTALLMENT TESTS (Section 22 & Section 45)
    # =========================================================================

    def test_22_installment_period_recognition_first_period(self):
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Apple Store",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("1000.00"),
            settlement_currency="CNY"
        )
        plan_id = uuid4()
        period_1_id = uuid4()
        cat_id = uuid4()
        plans = [{
            "id": plan_id,
            "credit_account_id": self.account_id,
            "total_periods": 12,
            "merchant": "Apple Store",
            "status": "pending_first_bill"
        }]
        periods = {
            plan_id: [
                {"id": period_1_id, "period_no": 1, "scheduled_amount": Decimal("1000.00"), "currency": "CNY", "status": "scheduled"},
                {"id": uuid4(), "period_no": 2, "scheduled_amount": Decimal("1000.00"), "currency": "CNY", "status": "scheduled"}
            ]
        }
        cand = process_installment_line(line, self.account_id, plans, periods, default_expense_category_id=cat_id)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.status, "accepted")
        self.assertEqual(cand.candidate_type, "recognize_installment")
        self.assertEqual(cand.payload["installment"]["period_no"], 1)
        self.assertTrue(cand.payload["installment"]["is_first_period"])
        self.assertFalse(cand.payload["installment"]["is_last_period"])

    def test_23_installment_remainder_allocation_recognition(self):
        line_p3 = NormalizedStatementLine(
            transaction_on=date(2026, 10, 10),
            description_raw="MacBook Air",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("3333.34"),
            settlement_currency="CNY"
        )
        plan_id = uuid4()
        period_3_id = uuid4()
        cat_id = uuid4()
        plans = [{
            "id": plan_id,
            "credit_account_id": self.account_id,
            "total_periods": 3,
            "merchant": "MacBook Air",
            "first_statement_month": date(2026, 8, 1),
            "status": "active"
        }]
        periods = {
            plan_id: [
                {"id": uuid4(), "period_no": 1, "scheduled_amount": Decimal("3333.33"), "currency": "CNY", "status": "billed"},
                {"id": uuid4(), "period_no": 2, "scheduled_amount": Decimal("3333.33"), "currency": "CNY", "status": "billed"},
                {"id": period_3_id, "period_no": 3, "scheduled_amount": Decimal("3333.34"), "currency": "CNY", "status": "scheduled"}
            ]
        }
        cand = process_installment_line(line_p3, self.account_id, plans, periods, default_expense_category_id=cat_id)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.status, "accepted")
        self.assertEqual(cand.payload["installment"]["period_no"], 3)
        self.assertTrue(cand.payload["installment"]["is_last_period"])

    def test_24_cancelled_plan_skipped(self):
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Apple Store",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("1000.00"),
            settlement_currency="CNY"
        )
        plan_id = uuid4()
        plans = [{
            "id": plan_id,
            "credit_account_id": self.account_id,
            "total_periods": 12,
            "merchant": "Apple Store",
            "status": "cancelled"
        }]
        periods = {plan_id: [{"id": uuid4(), "period_no": 1, "scheduled_amount": Decimal("1000.00"), "currency": "CNY", "status": "scheduled"}]}
        cand = process_installment_line(line, self.account_id, plans, periods)
        self.assertIsNone(cand)

    # =========================================================================
    # 8. RESIDUAL & SIMULATION TESTS (Section 25, 27, 28 & Section 46)
    # =========================================================================

    def test_25_explainable_missing_expense_explains_entire_residual(self):
        cand = CandidateProposal(
            candidate_type="create_transaction",
            status="accepted",
            payload={"transaction": {"amount": "500.00", "currency": "CNY", "transaction_type": "expense"}}
        )
        status, residual, adj = evaluate_residual_and_batch_readiness(
            baseline_projected_balance=Decimal("1500.00"),
            authoritative_balance=Decimal("1000.00"),
            candidates=[cand],
            account_id=self.account_id,
            account_currency="CNY"
        )
        self.assertEqual(residual, Decimal("0.00"))
        self.assertEqual(status, "ready")
        self.assertIsNone(adj)

    def test_26_small_unexplained_residual_auto_adjusts(self):
        status, residual, adj = evaluate_residual_and_batch_readiness(
            baseline_projected_balance=Decimal("1000.00"),
            authoritative_balance=Decimal("1047.00"),
            candidates=[],
            account_id=self.account_id,
            account_currency="CNY"
        )
        self.assertEqual(residual, Decimal("47.00"))
        self.assertEqual(status, "ready")
        self.assertIsNotNone(adj)
        self.assertEqual(adj.status, "accepted")
        self.assertEqual(adj.payload["adjustment_amount"], "47.00")

    def test_27_residual_boundary_200_cny(self):
        st_200, res_200, adj_200 = evaluate_residual_and_batch_readiness(
            baseline_projected_balance=Decimal("1000.00"),
            authoritative_balance=Decimal("1200.00"),
            candidates=[],
            account_id=self.account_id,
            account_currency="CNY"
        )
        self.assertEqual(st_200, "ready")
        self.assertEqual(adj_200.status, "accepted")

        st_200_01, res_200_01, adj_200_01 = evaluate_residual_and_batch_readiness(
            baseline_projected_balance=Decimal("1000.00"),
            authoritative_balance=Decimal("1200.01"),
            candidates=[],
            account_id=self.account_id,
            account_currency="CNY"
        )
        self.assertEqual(st_200_01, "needs_review")
        self.assertEqual(adj_200_01.status, "needs_review")
        self.assertEqual(adj_200_01.reason_code, RECONCILIATION_RESIDUAL_TOO_LARGE)

    def test_28_non_cny_residual_uses_cny_conversion(self):
        st_usd_20, _, adj_usd_20 = evaluate_residual_and_batch_readiness(
            baseline_projected_balance=Decimal("100.00"),
            authoritative_balance=Decimal("120.00"),
            candidates=[],
            account_id=self.usd_account_id,
            account_currency="USD",
            fx_rate_to_cny=Decimal("7.20")
        )
        self.assertEqual(st_usd_20, "ready")
        self.assertEqual(adj_usd_20.status, "accepted")

        st_usd_30, _, adj_usd_30 = evaluate_residual_and_batch_readiness(
            baseline_projected_balance=Decimal("100.00"),
            authoritative_balance=Decimal("130.00"),
            candidates=[],
            account_id=self.usd_account_id,
            account_currency="USD",
            fx_rate_to_cny=Decimal("7.20")
        )
        self.assertEqual(st_usd_30, "needs_review")
        self.assertEqual(adj_usd_30.status, "needs_review")
        self.assertEqual(adj_usd_30.reason_code, RECONCILIATION_RESIDUAL_TOO_LARGE)

    # =========================================================================
    # 9. CORRECTNESS FINAL-FIX REGRESSIONS (Items 1 - 12)
    # =========================================================================

    def test_29_hard_conflict_preserves_evidence_and_prevents_unmatched_creation(self):
        # Existing Starbucks 38 CNY vs Statement Starbucks 35 CNY (amount conflict)
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Starbucks Coffee",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("35.00"),
            settlement_currency="CNY"
        )
        tx_starbucks_38 = {
            "id": uuid4(),
            "occurred_on": date(2026, 8, 10),
            "from_account_id": self.account_id,
            "from_amount": Decimal("38.00"),
            "from_currency": "CNY",
            "merchant": "Starbucks Coffee",
            "transaction_type": "expense",
            "status": "committed"
        }
        res = run_deterministic_reconciliation(
            lines=[line],
            transactions=[tx_starbucks_38],
            selected_account_id=self.account_id,
            account_currency="CNY",
            baseline_projected_balance=Decimal("1000.00"),
            authoritative_balance=Decimal("965.00"),
            default_expense_category_id=uuid4()
        )
        self.assertEqual(res.batch_status, "needs_review")
        # Assert exactly one candidate exists (the conflict review candidate) and NO create_transaction candidate!
        create_cands = [c for c in res.candidates if c.candidate_type == "create_transaction"]
        self.assertEqual(len(create_cands), 0)
        match_cands = [c for c in res.candidates if c.candidate_type == "match"]
        self.assertEqual(len(match_cands), 1)
        self.assertEqual(match_cands[0].status, "needs_review")
        self.assertEqual(match_cands[0].reason_code, AMOUNT_CONFLICT)

    def test_30_foreign_original_amount_conflict_preserves_evidence(self):
        # Existing tx has original 10000 JPY, statement has original 12000 JPY
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Tokyo Hotel",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("560.00"),
            settlement_currency="CNY",
            original_amount=Decimal("12000.00"),
            original_currency="JPY"
        )
        tx_hotel = {
            "id": uuid4(),
            "occurred_on": date(2026, 8, 10),
            "from_account_id": self.account_id,
            "from_amount": Decimal("480.00"),
            "from_currency": "CNY",
            "original_amount": Decimal("10000.00"),
            "original_currency": "JPY",
            "merchant": "Tokyo Hotel",
            "transaction_type": "expense",
            "account_leg_status": "estimated",
            "status": "committed"
        }
        res = run_deterministic_reconciliation(
            lines=[line],
            transactions=[tx_hotel],
            selected_account_id=self.account_id,
            account_currency="CNY",
            baseline_projected_balance=Decimal("1000.00"),
            authoritative_balance=Decimal("440.00"),
            default_expense_category_id=uuid4()
        )
        self.assertEqual(res.batch_status, "needs_review")
        create_cands = [c for c in res.candidates if c.candidate_type == "create_transaction"]
        self.assertEqual(len(create_cands), 0)
        match_cands = [c for c in res.candidates if c.candidate_type == "match"]
        self.assertEqual(len(match_cands), 1)
        self.assertEqual(match_cands[0].reason_code, ORIGINAL_AMOUNT_CONFLICT)

    def test_31_strict_type_scoring_no_cross_type_points(self):
        line_expense = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Service Fee",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("50.00"),
            settlement_currency="CNY"
        )
        tx_fee = {
            "id": uuid4(),
            "occurred_on": date(2026, 8, 10),
            "from_account_id": self.account_id,
            "from_amount": Decimal("50.00"),
            "from_currency": "CNY",
            "merchant": "Service Fee",
            "transaction_type": "fee",
            "status": "committed"
        }
        score = compute_match_score(line_expense, tx_fee, self.account_id)
        # expense <-> fee must get type_score == 0
        self.assertEqual(score.type_score, 0)

        # fee <-> fee must get type_score == 10
        line_fee = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Service Fee",
            direction="debit",
            line_type="fee",
            settlement_amount=Decimal("50.00"),
            settlement_currency="CNY"
        )
        score_same = compute_match_score(line_fee, tx_fee, self.account_id)
        self.assertEqual(score_same.type_score, 10)

    def test_32_minor_unit_quantization_comparison(self):
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Supermarket",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("100.000"),
            settlement_currency="CNY"
        )
        tx = {
            "id": uuid4(),
            "occurred_on": date(2026, 8, 10),
            "from_account_id": self.account_id,
            "from_amount": Decimal("100.00"),
            "from_currency": "CNY",
            "merchant": "Supermarket",
            "transaction_type": "expense",
            "status": "committed"
        }
        score = compute_match_score(line, tx, self.account_id)
        self.assertEqual(score.amount_score, 40)
        self.assertFalse(score.is_blocked)

    def test_33_committed_cash_income_does_not_justify_transfer_creation(self):
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="转账",
            direction="debit",
            line_type="transfer",
            settlement_amount=Decimal("5000.00"),
            settlement_currency="CNY"
        )
        # Account B has committed cash_income +5000 without statement counter leg flag
        hh_movements = [{
            "account_id": self.counter_account_id,
            "direction": "credit",
            "amount": Decimal("5000.00"),
            "currency": "CNY",
            "occurred_on": date(2026, 8, 10),
            "is_counter_statement_leg": False
        }]
        cand = process_transfer_line(line, self.account_id, [], hh_movements)
        self.assertEqual(cand.status, "needs_review")
        self.assertEqual(cand.reason_code, COUNTER_ACCOUNT_UNRESOLVED)

    def test_34_weak_refund_similarity_triggers_needs_review(self):
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Apple Store Online",
            direction="credit",
            line_type="refund",
            settlement_amount=Decimal("300.00"),
            settlement_currency="CNY"
        )
        orig_exp_id = uuid4()
        candidate_expenses = [{
            "id": orig_exp_id,
            "occurred_on": date(2026, 7, 15),
            "from_account_id": self.account_id,
            "from_amount": Decimal("1000.00"),
            "from_currency": "CNY",
            "merchant": "Apple Store",
            "transaction_type": "expense",
            "status": "committed"
        }]
        cand = process_refund_line(line, self.account_id, candidate_expenses, {})
        # Similarity is between 0.40 and 0.80 -> needs_review
        self.assertEqual(cand.status, "needs_review")
        self.assertEqual(cand.reason_code, "MERCHANT_WEAK_MATCH")


    def test_35_installment_wrong_month_not_recognized(self):
        # Period 2 expected in Sep 2026, statement line is in Nov 2026
        line_nov = NormalizedStatementLine(
            transaction_on=date(2026, 11, 10),
            description_raw="MacBook Air",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("3333.33"),
            settlement_currency="CNY"
        )
        plan_id = uuid4()
        plans = [{
            "id": plan_id,
            "credit_account_id": self.account_id,
            "total_periods": 3,
            "merchant": "MacBook Air",
            "first_statement_month": date(2026, 8, 1),
            "status": "active"
        }]
        periods = {
            plan_id: [
                {"id": uuid4(), "period_no": 1, "scheduled_amount": Decimal("3333.33"), "currency": "CNY", "status": "billed"},
                {"id": uuid4(), "period_no": 2, "scheduled_amount": Decimal("3333.33"), "currency": "CNY", "status": "scheduled"},
                {"id": uuid4(), "period_no": 3, "scheduled_amount": Decimal("3333.34"), "currency": "CNY", "status": "scheduled"}
            ]
        }
        cand = process_installment_line(line_nov, self.account_id, plans, periods, default_expense_category_id=uuid4())
        self.assertIsNone(cand)

    def test_36_installment_missing_category_needs_review(self):
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Apple Store",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("1000.00"),
            settlement_currency="CNY"
        )
        plan_id = uuid4()
        plans = [{
            "id": plan_id,
            "credit_account_id": self.account_id,
            "total_periods": 12,
            "merchant": "Apple Store",
            "status": "pending_first_bill"
        }]
        periods = {plan_id: [{"id": uuid4(), "period_no": 1, "scheduled_amount": Decimal("1000.00"), "currency": "CNY", "status": "scheduled"}]}
        cand = process_installment_line(line, self.account_id, plans, periods, default_expense_category_id=None)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.status, "needs_review")
        self.assertEqual(cand.reason_code, "CATEGORY_REQUIRED")

    def test_37_unknown_debit_and_no_date_lines_trigger_needs_review(self):
        line_unknown_type = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Unspecified Movement",
            direction="debit",
            line_type="unknown",
            settlement_amount=Decimal("100.00"),
            settlement_currency="CNY"
        )
        line_no_date = NormalizedStatementLine(
            transaction_on=None,
            posted_on=None,
            description_raw="Coffee Shop",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("35.00"),
            settlement_currency="CNY"
        )
        res = run_deterministic_reconciliation(
            lines=[line_unknown_type, line_no_date],
            transactions=[],
            selected_account_id=self.account_id,
            account_currency="CNY",
            baseline_projected_balance=Decimal("1000.00"),
            authoritative_balance=Decimal("865.00"),
            default_expense_category_id=uuid4()
        )
        self.assertEqual(res.batch_status, "needs_review")
        self.assertTrue(all(c.status == "needs_review" for c in res.candidates))

    def test_38_input_line_order_permutation_invariance(self):
        cat_id = uuid4()
        line_a = NormalizedStatementLine(
            transaction_on=date(2026, 8, 1),
            description_raw="Alpha Store",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("10.00"),
            settlement_currency="CNY"
        )
        line_b = NormalizedStatementLine(
            transaction_on=date(2026, 8, 5),
            description_raw="Beta Store",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("20.00"),
            settlement_currency="CNY"
        )
        line_c = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Gamma Store",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("30.00"),
            settlement_currency="CNY"
        )
        res_order1 = run_deterministic_reconciliation(
            lines=[line_a, line_b, line_c],
            transactions=[],
            selected_account_id=self.account_id,
            account_currency="CNY",
            baseline_projected_balance=Decimal("1000.00"),
            authoritative_balance=Decimal("940.00"),
            default_expense_category_id=cat_id
        )
        res_order2 = run_deterministic_reconciliation(
            lines=[line_c, line_a, line_b],
            transactions=[],
            selected_account_id=self.account_id,
            account_currency="CNY",
            baseline_projected_balance=Decimal("1000.00"),
            authoritative_balance=Decimal("940.00"),
            default_expense_category_id=cat_id
        )
        self.assertEqual(res_order1.batch_status, res_order2.batch_status)
        self.assertEqual(res_order1.residual_amount, res_order2.residual_amount)
        self.assertEqual(res_order1.matched_count, res_order2.matched_count)
        self.assertEqual(res_order1.created_count, res_order2.created_count)

    def test_39_authoritative_data_conflict_runtime_path(self):
        """
        Regression for Item 1:
        Existing transaction is statement_confirmed with authoritative 100 CNY.
        New statement line has same date/merchant but 120 CNY.
        Must produce needs_review with AUTHORITATIVE_DATA_CONFLICT without exception or duplicate.
        """
        from app.domain.reconciliation.models import AUTHORITATIVE_DATA_CONFLICT
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Starbucks Coffee",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("120.00"),
            settlement_currency="CNY"
        )
        tx_id = uuid4()
        existing_txs = [{
            "id": tx_id,
            "occurred_on": date(2026, 8, 10),
            "from_account_id": self.account_id,
            "from_amount": Decimal("100.00"),
            "from_currency": "CNY",
            "merchant": "Starbucks Coffee",
            "transaction_type": "expense",
            "account_leg_status": "authoritative",
            "verification_status": "statement_confirmed",
            "status": "committed"
        }]
        res = run_deterministic_reconciliation(
            lines=[line],
            transactions=existing_txs,
            selected_account_id=self.account_id,
            account_currency="CNY",
            baseline_projected_balance=Decimal("1000.00"),
            authoritative_balance=Decimal("880.00"),
            default_expense_category_id=uuid4()
        )
        self.assertEqual(res.batch_status, "needs_review")
        self.assertEqual(res.created_count, 0)
        self.assertFalse(any(c.candidate_type == "create_transaction" for c in res.candidates))
        match_cands = [c for c in res.candidates if c.candidate_type == "match"]
        self.assertEqual(len(match_cands), 1)
        cand = match_cands[0]
        self.assertEqual(cand.status, "needs_review")
        self.assertEqual(cand.reason_code, AUTHORITATIVE_DATA_CONFLICT)
        self.assertEqual(cand.target_transaction_id, tx_id)


    def test_40_foreign_card_estimated_settlement_residual_simulation(self):
        """
        Regression for Item 9:
        Ledger has estimated expense 68.90 USD (baseline projected balance = 1931.10 USD).
        Statement has settlement 68.20 USD, authoritative balance = 1931.80 USD.
        During residual simulation, signed delta (+0.70) is simulated.
        Resulting residual = 0.00, batch is ready, NO reconciliation adjustment created!
        """
        line = NormalizedStatementLine(
            transaction_on=date(2026, 8, 10),
            description_raw="Tokyo Hotel JPY",
            direction="debit",
            line_type="expense",
            settlement_amount=Decimal("68.20"),
            settlement_currency="USD",
            original_amount=Decimal("10000.00"),
            original_currency="JPY"
        )
        tx_id = uuid4()
        existing_txs = [{
            "id": tx_id,
            "occurred_on": date(2026, 8, 10),
            "from_account_id": self.account_id,
            "from_amount": Decimal("68.90"),
            "from_currency": "USD",
            "original_amount": Decimal("10000.00"),
            "original_currency": "JPY",
            "merchant": "Tokyo Hotel JPY",
            "transaction_type": "expense",
            "account_leg_status": "estimated",
            "verification_status": "unverified",
            "status": "committed"
        }]
        res = run_deterministic_reconciliation(
            lines=[line],
            transactions=existing_txs,
            selected_account_id=self.account_id,
            account_currency="USD",
            baseline_projected_balance=Decimal("1931.10"),
            authoritative_balance=Decimal("1931.80"),
            default_expense_category_id=uuid4()
        )
        self.assertEqual(res.batch_status, "ready")
        self.assertEqual(res.residual_amount, Decimal("0.00"))
        self.assertIsNone(res.adjustment_amount)
        self.assertEqual(len(res.candidates), 1)
        self.assertEqual(res.candidates[0].candidate_type, "match")
        self.assertEqual(res.candidates[0].status, "accepted")
        self.assertIn("settlement_patch", res.candidates[0].payload)


if __name__ == "__main__":
    unittest.main()

