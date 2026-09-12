import unittest
from uuid import uuid4
from datetime import date
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.routes.accounts import PatchAccountRequest
from app.services.account_commands import compute_command_hash
from app.api.errors import extract_error_details, build_error_response
from app.domain.transactions import (
    AccountNameConflictError,
    RowVersionConflictError,
    AccountResourceNotFoundError,
    IdempotencyKeyReuseError,
    CurrencyImmutableError,
)


class TestAccountCommandsUnit(unittest.TestCase):
    """
    Unit tests for Stage 2A account command identity and error extraction contracts.
    """

    def test_patch_account_request_field_presence_inventory(self):
        """
        Verifies that exclude_unset=True systematically preserves field presence distinctions
        across all fields of PatchAccountRequest.
        """
        acc_id = uuid4()
        user_id = uuid4()
        op = f"PATCH /api/v1/accounts/{acc_id}"

        # Baseline: only expected_version
        base_req = PatchAccountRequest.model_validate({"expected_version": 0})
        base_dump = base_req.model_dump(mode="json", exclude_unset=True)
        self.assertEqual(base_dump, {"expected_version": 0})
        base_hash = compute_command_hash(op, base_dump)

        # 1. risk_level omitted vs explicit null vs explicit value
        risk_null_req = PatchAccountRequest.model_validate({"expected_version": 0, "risk_level": None})
        risk_null_dump = risk_null_req.model_dump(mode="json", exclude_unset=True)
        self.assertEqual(risk_null_dump, {"expected_version": 0, "risk_level": None})
        risk_null_hash = compute_command_hash(op, risk_null_dump)

        risk_val_req = PatchAccountRequest.model_validate({"expected_version": 0, "risk_level": "low"})
        risk_val_dump = risk_val_req.model_dump(mode="json", exclude_unset=True)
        risk_val_hash = compute_command_hash(op, risk_val_dump)

        self.assertNotEqual(base_hash, risk_null_hash, "Omitted risk_level and explicit null must have different hashes")
        self.assertNotEqual(base_hash, risk_val_hash, "Omitted risk_level and explicit value must have different hashes")
        self.assertNotEqual(risk_null_hash, risk_val_hash, "Explicit null and explicit value must have different hashes")

        # 2. owner_user_id omitted vs explicit null vs explicit value
        owner_null_req = PatchAccountRequest.model_validate({"expected_version": 0, "owner_user_id": None})
        owner_null_dump = owner_null_req.model_dump(mode="json", exclude_unset=True)
        self.assertEqual(owner_null_dump, {"expected_version": 0, "owner_user_id": None})
        owner_null_hash = compute_command_hash(op, owner_null_dump)

        owner_val_req = PatchAccountRequest.model_validate({"expected_version": 0, "owner_user_id": str(user_id)})
        owner_val_dump = owner_val_req.model_dump(mode="json", exclude_unset=True)
        owner_val_hash = compute_command_hash(op, owner_val_dump)

        self.assertNotEqual(base_hash, owner_null_hash, "Omitted owner_user_id and explicit null must have different hashes")
        self.assertNotEqual(base_hash, owner_val_hash, "Omitted owner_user_id and explicit value must have different hashes")
        self.assertNotEqual(owner_null_hash, owner_val_hash, "Explicit null and explicit value must have different hashes")

        # 3. name omitted vs explicit value
        name_val_req = PatchAccountRequest.model_validate({"expected_version": 0, "name": "Checking"})
        name_val_dump = name_val_req.model_dump(mode="json", exclude_unset=True)
        name_val_hash = compute_command_hash(op, name_val_dump)
        self.assertNotEqual(base_hash, name_val_hash)

        # 4. balance_scope omitted vs explicit value
        scope_val_req = PatchAccountRequest.model_validate({"expected_version": 0, "balance_scope": "Liquid"})
        scope_val_dump = scope_val_req.model_dump(mode="json", exclude_unset=True)
        scope_val_hash = compute_command_hash(op, scope_val_dump)
        self.assertNotEqual(base_hash, scope_val_hash)

        # 5. statement_import_enabled omitted vs explicit value
        stmt_val_req = PatchAccountRequest.model_validate({"expected_version": 0, "statement_import_enabled": True})
        stmt_val_dump = stmt_val_req.model_dump(mode="json", exclude_unset=True)
        stmt_val_hash = compute_command_hash(op, stmt_val_dump)
        self.assertNotEqual(base_hash, stmt_val_hash)

        # 6. opened_on omitted vs explicit value
        opened_val_req = PatchAccountRequest.model_validate({"expected_version": 0, "opened_on": "2026-01-01"})
        opened_val_dump = opened_val_req.model_dump(mode="json", exclude_unset=True)
        opened_val_hash = compute_command_hash(op, opened_val_dump)
        self.assertNotEqual(base_hash, opened_val_hash)

        # 7. reason omitted vs explicit value
        reason_val_req = PatchAccountRequest.model_validate({"expected_version": 0, "reason": "Correction"})
        reason_val_dump = reason_val_req.model_dump(mode="json", exclude_unset=True)
        reason_val_hash = compute_command_hash(op, reason_val_dump)
        self.assertNotEqual(base_hash, reason_val_hash)

        # Exact retry determinism
        base_req_retry = PatchAccountRequest.model_validate({"expected_version": 0})
        base_retry_hash = compute_command_hash(op, base_req_retry.model_dump(mode="json", exclude_unset=True))
        self.assertEqual(base_hash, base_retry_hash)

    def test_extract_error_details_canonical_mapping(self):
        """
        Validates extract_error_details produces identical HTTP status codes, error codes,
        and JSON body shapes for HTTPException and LedgerDomainError subclasses.
        """
        # 1. HTTP 400 rejection
        exc_400 = HTTPException(status_code=400, detail="Closing snapshot proves non-zero balance.")
        status_code, code, payload = extract_error_details(exc_400)
        self.assertEqual(status_code, 400)
        self.assertEqual(code, "BAD_REQUEST")
        self.assertEqual(payload, {
            "error": {
                "code": "BAD_REQUEST",
                "message": "Closing snapshot proves non-zero balance.",
                "retryable": False,
                "details": {}
            },
            "detail": "Closing snapshot proves non-zero balance."
        })

        # 2. HTTP 404
        exc_404 = HTTPException(status_code=404, detail="Account not found.")
        status_code, code, payload = extract_error_details(exc_404)
        self.assertEqual(status_code, 404)
        self.assertEqual(code, "NOT_FOUND")

        # 3. HTTP 409
        exc_409 = HTTPException(status_code=409, detail="Conflict occurred.")
        status_code, code, payload = extract_error_details(exc_409)
        self.assertEqual(status_code, 409)
        self.assertEqual(code, "CONFLICT")

        # 4. Domain error: AccountNameConflictError (422)
        exc_name = AccountNameConflictError("Checking")
        status_code, code, payload = extract_error_details(exc_name)
        self.assertEqual(status_code, 422)
        self.assertEqual(code, "ACCOUNT_NAME_CONFLICT")
        self.assertIn("Checking", payload["error"]["message"])

        # 5. Domain error: RowVersionConflictError (409)
        exc_ver = RowVersionConflictError()
        status_code, code, payload = extract_error_details(exc_ver)
        self.assertEqual(status_code, 409)
        self.assertEqual(code, "ROW_VERSION_CONFLICT")

        # 6. Domain error: AccountResourceNotFoundError (404)
        acc_id = uuid4()
        exc_res = AccountResourceNotFoundError(acc_id)
        status_code, code, payload = extract_error_details(exc_res)
        self.assertEqual(status_code, 404)
        self.assertEqual(code, "ACCOUNT_NOT_FOUND")

        # 7. Generic Exception (500)
        exc_500 = RuntimeError("Database crash")
        status_code, code, payload = extract_error_details(exc_500)
        self.assertEqual(status_code, 500)
        self.assertEqual(code, "INTERNAL_SERVER_ERROR")
        self.assertTrue(payload["error"]["retryable"])
