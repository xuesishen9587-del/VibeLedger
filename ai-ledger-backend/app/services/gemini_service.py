import os
import json
import base64
from typing import Optional, Dict, Any, List, Union
from decimal import Decimal
from datetime import date
from pydantic import BaseModel, Field
from app.domain.transactions import GeminiDependencyError

class ExpenseFieldConfidenceTransport(BaseModel):
    """
    Explicit fixed-field transport model for field-level confidence scores.
    Uses static fields to eliminate dynamic dictionary schema extensions.
    """
    amount: Optional[float] = None
    currency: Optional[float] = None
    account: Optional[float] = None
    category: Optional[float] = None
    date: Optional[float] = None
    total_periods: Optional[float] = None


class ExpenseExtractionTransportSchema(BaseModel):
    """
    Strict transport schema passed as response_schema to Google Gemini Developer API.
    Contains only explicit, static fields supported across all Gemini API modes.
    """
    occurred_on: Optional[date] = None
    merchant: Optional[str] = None
    original_amount: Optional[Decimal] = None
    original_currency: Optional[str] = None  # MUST NOT default to "CNY"
    from_account: Optional[str] = None
    category: Optional[str] = None
    payment_mode: Optional[str] = None  # MUST NOT default to "one_off"; defaults to None
    total_amount: Optional[Decimal] = None
    total_periods: Optional[int] = None
    confidence: Optional[float] = 1.0
    field_confidence: Optional[ExpenseFieldConfidenceTransport] = None


class ExpenseExtractionResult(BaseModel):
    """
    Internal domain extraction result consumed by expense_service.py.
    Retains field_confidence as Dict[str, float] for flexible downstream .get(...) lookups.
    """
    occurred_on: Optional[date] = None
    merchant: Optional[str] = None
    original_amount: Optional[Decimal] = None
    original_currency: Optional[str] = None  # MUST NOT default to "CNY"
    from_account: Optional[str] = None
    category: Optional[str] = None
    payment_mode: Optional[str] = None  # MUST NOT default to "one_off"; defaults to None
    total_amount: Optional[Decimal] = None
    total_periods: Optional[int] = None
    confidence: float = 1.0
    field_confidence: Dict[str, float] = Field(default_factory=dict)
    raw_response: Optional[Dict[str, Any]] = None

    @classmethod
    def from_transport(
        cls,
        transport: ExpenseExtractionTransportSchema,
        raw_response: Optional[Dict[str, Any]] = None
    ) -> "ExpenseExtractionResult":
        field_conf: Dict[str, float] = {}
        if transport.field_confidence is not None:
            fc = transport.field_confidence
            for k in ("amount", "currency", "account", "category", "date", "total_periods"):
                v = getattr(fc, k, None)
                if v is not None:
                    field_conf[k] = float(v)

        return cls(
            occurred_on=transport.occurred_on,
            merchant=transport.merchant,
            original_amount=transport.original_amount,
            original_currency=transport.original_currency,
            from_account=transport.from_account,
            category=transport.category,
            payment_mode=transport.payment_mode,
            total_amount=transport.total_amount,
            total_periods=transport.total_periods,
            confidence=float(transport.confidence) if transport.confidence is not None else 0.0,
            field_confidence=field_conf,
            raw_response=raw_response
        )

class GeminiService:
    """
    Expense-only extraction service interface.
    Extracts structured expense or installment facts from screenshot and note.
    """
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")

    def build_system_prompt(
        self,
        accounts: List[Dict[str, Any]],
        categories: List[Dict[str, Any]]
    ) -> str:
        acc_descriptions = []
        for a in accounts:
            aliases_str = f" (aliases: {', '.join(a.get('aliases', []))})" if a.get('aliases') else ""
            acc_descriptions.append(f"- {a['name']} [{a['account_type']}, {a['currency']}]{aliases_str}")

        cat_descriptions = [f"- {c['name']}" for c in categories if c.get("category_type") == "expense"]

        return f"""
You are an expert, precise personal expense receipt extractor.
Your SOLE task is to extract expense transaction details from the provided screenshot and user note.
Do NOT attempt to classify transfers, income, or investment adjustments. All submissions to this pipeline are expenses.

AVAILABLE HOUSEHOLD ACCOUNTS:
{chr(10).join(acc_descriptions) if acc_descriptions else "No specific accounts configured."}

AVAILABLE EXPENSE CATEGORIES:
{chr(10).join(cat_descriptions) if cat_descriptions else "No specific categories configured."}

EXTRACTION RULES:
1. occurred_on: Extract the actual business transaction date (YYYY-MM-DD). If unclear or not visible, use null.
2. merchant: The store, vendor, platform, or payee name.
3. original_amount: The exact total consumption amount charged.
4. original_currency: 3-letter currency code (e.g. CNY, USD, JPY, EUR). If currency is not explicitly clear, set to null. DO NOT default to CNY.
5. from_account: The name of the payment card, bank account, or wallet used. Match closely with available accounts or aliases.
6. category: The best matching expense category name from the available categories.
7. payment_mode: Set to "installment" if the receipt explicitly shows a credit card installment purchase (e.g. 分期, split into N periods/months); otherwise "one_off". If unclear, use null.
8. If payment_mode is "installment":
   - total_amount: Total principal amount to be amortized.
   - total_periods: Total number of installment months/periods (e.g. 3, 6, 12, 24). Must be null if not explicitly stated.
   - merchant: Merchant name.
   - from_account: Paying credit card account name.
9. confidence: Overall extraction confidence score between 0.0 and 1.0.
10. field_confidence: Object containing confidence scores (0.0 to 1.0) for individual fields: amount, currency, account, category, date, total_periods.
"""

    def extract_expense(
        self,
        image_bytes: bytes,
        mime_type: str,
        note: Optional[str],
        accounts: List[Dict[str, Any]],
        categories: List[Dict[str, Any]],
        captured_at: Optional[Any] = None
    ) -> ExpenseExtractionResult:
        if not self.api_key:
            raise GeminiDependencyError("GEMINI_API_KEY is not configured.")

        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=self.api_key)
            system_prompt = self.build_system_prompt(accounts, categories)

            prompt_text = f"Extract expense details from this image. User note: '{note or ''}'."
            if captured_at:
                prompt_text += f" Captured at: {captured_at}."

            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    prompt_text
                ],
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    response_schema=ExpenseExtractionTransportSchema,
                    temperature=0.1
                )
            )

            data = json.loads(response.text, parse_float=Decimal)
            transport = ExpenseExtractionTransportSchema.model_validate(data)
            return ExpenseExtractionResult.from_transport(transport, raw_response=data)
        except Exception as e:
            raise GeminiDependencyError(f"AI extraction service failed: {e}")


class MockGeminiService(GeminiService):
    """
    Deterministic mock for automated testing.
    """
    def __init__(self, default_result: Optional[ExpenseExtractionResult] = None):
        super().__init__(api_key="mock_key")
        self.default_result = default_result or ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="Test Merchant",
            original_amount=Decimal("268.00"),
            original_currency="CNY",
            from_account=None,
            category=None,
            payment_mode="one_off",
            confidence=1.0,
            field_confidence={"amount": 1.0, "currency": 1.0, "account": 1.0, "category": 1.0, "date": 1.0}
        )
        self._custom_responses: List[ExpenseExtractionResult] = []
        self.call_count: int = 0
        self.should_raise: Optional[Exception] = None

    def set_next_result(self, result: ExpenseExtractionResult) -> None:
        self._custom_responses.append(result)

    def extract_expense(
        self,
        image_bytes: bytes,
        mime_type: str,
        note: Optional[str],
        accounts: List[Dict[str, Any]],
        categories: List[Dict[str, Any]],
        captured_at: Optional[Any] = None
    ) -> ExpenseExtractionResult:
        self.call_count += 1
        if self.should_raise:
            raise self.should_raise
        if self._custom_responses:
            return self._custom_responses.pop(0)
        return self.default_result
