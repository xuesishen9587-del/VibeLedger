from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Optional, Dict, Any, List
from uuid import UUID, uuid4

from app.domain.money import parse_decimal, validate_currency_code, quantize_money

# Frozen constants
MATCH_DATE_WINDOW_DAYS: int = 5
AUTO_MATCH_SCORE: int = 80
AUTO_MATCH_MARGIN: int = 15

MERCHANT_STRONG_SIMILARITY: Decimal = Decimal("0.80")
MERCHANT_MEDIUM_SIMILARITY: Decimal = Decimal("0.60")
MERCHANT_WEAK_SIMILARITY: Decimal = Decimal("0.40")

REFUND_LOOKBACK_DAYS: int = 180
AUTO_ADJUST_THRESHOLD_CNY: Decimal = Decimal("200.00")

# Frozen reason codes
NO_MATCH = "NO_MATCH"
LOW_MATCH_SCORE = "LOW_MATCH_SCORE"
MULTIPLE_TRANSACTION_MATCHES = "MULTIPLE_TRANSACTION_MATCHES"
TRANSACTION_ALREADY_CLAIMED = "TRANSACTION_ALREADY_CLAIMED"
AMOUNT_CONFLICT = "AMOUNT_CONFLICT"
ORIGINAL_AMOUNT_CONFLICT = "ORIGINAL_AMOUNT_CONFLICT"
DATE_OUTSIDE_WINDOW = "DATE_OUTSIDE_WINDOW"
MERCHANT_WEAK_MATCH = "MERCHANT_WEAK_MATCH"

ACCOUNT_UNRESOLVED = "ACCOUNT_UNRESOLVED"
COUNTER_ACCOUNT_UNRESOLVED = "COUNTER_ACCOUNT_UNRESOLVED"
CROSS_CURRENCY_MISSING_LEG = "CROSS_CURRENCY_MISSING_LEG"

TYPE_AMBIGUOUS = "TYPE_AMBIGUOUS"
INCOME_TRANSFER_REFUND_AMBIGUOUS = "INCOME_TRANSFER_REFUND_AMBIGUOUS"

REFUND_ORIGINAL_NOT_FOUND = "REFUND_ORIGINAL_NOT_FOUND"
MULTIPLE_REFUND_ORIGINALS = "MULTIPLE_REFUND_ORIGINALS"
REFUND_EXCEEDS_ORIGINAL = "REFUND_EXCEEDS_ORIGINAL"

INSTALLMENT_PLAN_AMBIGUOUS = "INSTALLMENT_PLAN_AMBIGUOUS"

RECONCILIATION_RESIDUAL_TOO_LARGE = "RECONCILIATION_RESIDUAL_TOO_LARGE"
AUTHORITATIVE_DATA_CONFLICT = "AUTHORITATIVE_DATA_CONFLICT"
SETTLEMENT_DEVIATION_SUSPICIOUS = "SETTLEMENT_DEVIATION_SUSPICIOUS"

VALID_DIRECTIONS = {"debit", "credit", "unknown"}
VALID_LINE_TYPES = {"expense", "income", "transfer", "refund", "fee", "unknown"}
VALID_MATCH_STATUSES = {"unmatched", "matched", "new_candidate", "ambiguous", "ignored"}
VALID_CANDIDATE_TYPES = {
    "match",
    "create_transaction",
    "create_transfer",
    "refund",
    "adjustment",
    "snapshot",
    "investment_pnl",
    "recognize_installment"
}
VALID_CANDIDATE_STATUSES = {"proposed", "needs_review", "accepted", "rejected", "applied"}


@dataclass
class NormalizedStatementLine:
    description_raw: str
    direction: str
    line_type: str
    settlement_amount: Decimal
    settlement_currency: str
    id: Optional[UUID] = None
    transaction_on: Optional[date] = None
    posted_on: Optional[date] = None
    description_normalized: Optional[str] = None
    original_amount: Optional[Decimal] = None
    original_currency: Optional[str] = None
    merchant_hint: Optional[str] = None
    external_reference: Optional[str] = None
    confidence: Optional[Decimal] = None
    source_page_no: Optional[int] = None
    source_row_no: Optional[int] = None
    line_fingerprint: Optional[bytes] = None

    def __post_init__(self):
        if not self.id:
            self.id = uuid4()
        if self.direction not in VALID_DIRECTIONS:
            raise ValueError(f"Invalid direction: {self.direction}. Allowed: {VALID_DIRECTIONS}")
        if self.line_type not in VALID_LINE_TYPES:
            raise ValueError(f"Invalid line_type: {self.line_type}. Allowed: {VALID_LINE_TYPES}")
        
        self.settlement_amount = parse_decimal(self.settlement_amount)
        if self.settlement_amount <= 0:
            raise ValueError("settlement_amount must be strictly positive")
        self.settlement_currency = validate_currency_code(self.settlement_currency)
        self.settlement_amount = quantize_money(self.settlement_amount, self.settlement_currency)

        if self.original_amount is not None:
            self.original_amount = parse_decimal(self.original_amount)
            if self.original_amount <= 0:
                raise ValueError("original_amount must be strictly positive")
            if self.original_currency is not None:
                self.original_currency = validate_currency_code(self.original_currency)
                self.original_amount = quantize_money(self.original_amount, self.original_currency)

        if self.confidence is not None:
            self.confidence = parse_decimal(self.confidence)
            if not (Decimal("0.0000") <= self.confidence <= Decimal("1.0000")):
                raise ValueError("confidence must be between 0 and 1")

    @property
    def effective_date(self) -> Optional[date]:
        return self.transaction_on if self.transaction_on is not None else self.posted_on


@dataclass
class MatchScore:
    amount_score: int = 0
    date_score: int = 0
    merchant_score: int = 0
    type_score: int = 0
    extra_score: int = 0
    total_score: int = 0
    is_blocked: bool = False
    block_reason: Optional[str] = None
    date_diff_days: Optional[int] = None
    merchant_similarity: Decimal = Decimal("0.00")


@dataclass
class ScoredCandidate:
    statement_line: NormalizedStatementLine
    transaction: Dict[str, Any]
    score: MatchScore


@dataclass
class CandidateProposal:
    candidate_type: str
    status: str
    statement_line_id: Optional[UUID] = None
    target_transaction_id: Optional[UUID] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    confidence: Optional[Decimal] = None
    reason_code: Optional[str] = None
    reason_detail: Optional[str] = None
    id: Optional[UUID] = None

    def __post_init__(self):
        if not self.id:
            self.id = uuid4()
        if self.candidate_type not in VALID_CANDIDATE_TYPES:
            raise ValueError(f"Invalid candidate_type: {self.candidate_type}")
        if self.status not in VALID_CANDIDATE_STATUSES:
            raise ValueError(f"Invalid status: {self.status}")


@dataclass
class ReconciliationResult:
    batch_status: str  # 'ready' or 'needs_review'
    candidates: List[CandidateProposal]
    matched_count: int
    created_count: int
    pending_count: int
    residual_amount: Decimal
    adjustment_amount: Optional[Decimal] = None
    engine_version: str = "v1.0.0"
