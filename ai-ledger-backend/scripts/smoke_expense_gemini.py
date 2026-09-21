"""Opt-in live Gemini smoke: sends one screenshot, never accesses the ledger DB."""
import argparse
import base64
from datetime import date
from decimal import Decimal
import logging
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domain.capture_images import decode_image, MAX_BYTES
from app.services.gemini_diagnostics import request_context
from app.services.gemini_service import GeminiService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--amount", type=Decimal, required=True)
    parser.add_argument("--currency", required=True)
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--merchant", required=True)
    parser.add_argument("--revision", action="store_true", help="Also verify a synthetic amount correction")
    args = parser.parse_args()
    # Keep SDK/HTTP loggers quiet. Our logger emits only allowlisted metadata.
    logging.basicConfig(level=logging.CRITICAL)
    logging.getLogger("app.gemini").setLevel(logging.WARNING)
    identity = str(uuid4())
    print(f"Live expense transport smoke diagnostic_id={identity}")
    try:
        if args.image.stat().st_size > MAX_BYTES:
            raise ValueError()
        mime = "image/png" if args.image.suffix.lower() == ".png" else "image/jpeg"
        raw, mime = decode_image({"mime_type": mime, "base64": base64.b64encode(args.image.read_bytes()).decode("ascii")})
        service = GeminiService()
        with request_context(identity):
            result = service.extract_expense(raw, mime, None, [], [])
            matches = (result.original_amount == args.amount and result.original_currency == args.currency
                       and result.occurred_on == args.date and result.merchant == args.merchant
                       and result.intent == "expense" and result.date_evidence == "visible")
            if not matches:
                print("FAIL: expected screenshot facts/intent did not match; no model response printed.")
                return 1
            if args.revision:
                revision = service.revise_expense_draft(result.model_dump(mode="json", exclude={"raw_response"}),
                    "Correct only the amount to 13.25. Leave all other fields unchanged.", [], [])
                fields = revision.model_dump(exclude_none=True, exclude={"raw_response"})
                if fields != {"original_amount": Decimal("13.25")}:
                    print("FAIL: revision did not preserve unspecified fields.")
                    return 1
        print("PASS: live Gemini transport and strict local validation; no ledger writes.")
        return 0
    except Exception:
        # Service failures have a safe diagnostic above; no exception body/traceback.
        print("FAIL: check image/configuration and app.gemini metadata for this diagnostic_id.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
