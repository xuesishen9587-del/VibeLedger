import os
import requests
from typing import Optional, Dict, Any, List, Union
from decimal import Decimal

from time_utils import format_iso_timestamp


# --- Structured API Error Exceptions ---

class ApiError(Exception):
    """Base API exception with structured error envelope fields."""
    def __init__(
        self,
        message: str,
        code: str = "API_ERROR",
        status_code: int = 500,
        details: Optional[Dict[str, Any]] = None,
        retryable: bool = False
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details = details or {}
        self.retryable = retryable

    def __str__(self):
        return f"[{self.code}] {self.message}"


class AuthError(ApiError):
    """401 Unauthorized / Session Expired / Missing Token"""
    pass


class ForbiddenError(ApiError):
    """403 Forbidden / Cross-household permission denied"""
    pass


class NotFoundError(ApiError):
    """404 Resource Not Found"""
    pass


class ConflictError(ApiError):
    """409 Conflict / ROW_VERSION_CONFLICT / BATCH_VERSION_CONFLICT / IDEMPOTENCY_KEY_REUSE"""
    pass


class ValidationError(ApiError):
    """422 Unprocessable Entity / Deterministic validation failure"""
    pass


class ServiceUnavailableError(ApiError):
    """503 Service Unavailable / Dependency unavailable"""
    pass


class BackendUnavailableError(ApiError):
    """Network connection error / Backend unreachable"""
    def __init__(self, message: str = "Backend service is unreachable. Please verify BACKEND_URL."):
        super().__init__(message, code="BACKEND_UNAVAILABLE", status_code=503, retryable=True)


class TimeoutError(ApiError):
    """Request timeout"""
    def __init__(self, message: str = "Request to backend service timed out."):
        super().__init__(message, code="TIMEOUT", status_code=504, retryable=True)


# --- API Client Implementation ---

class ApiClient:
    """
    Dedicated HTTP API Client for VibeLedger Dashboard.
    Owns all HTTP communication against Backend /api/v1/* REST APIs.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        auth_token: Optional[str] = None,
        timeout: float = 15.0
    ):
        self.base_url = (base_url or os.environ.get("BACKEND_URL", "http://localhost:8000")).rstrip("/")
        self.auth_token = auth_token or os.environ.get("AUTH_TOKEN")
        self.timeout = timeout
        self.session = requests.Session()

    def set_auth_token(self, token: Optional[str]) -> None:
        """Sets or updates the active Browser JWT authentication token."""
        self.auth_token = token

    def _get_headers(self, custom_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
        }
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        if custom_headers:
            headers.update(custom_headers)
        return headers

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Any] = None,
        data: Optional[Any] = None,
        files: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None
    ) -> Any:
        """
        Executes an HTTP request against the backend and parses structured responses and error envelopes.
        """
        url = f"{self.base_url}{path}"
        req_headers = self._get_headers(headers)
        req_timeout = timeout if timeout is not None else self.timeout

        try:
            resp = self.session.request(
                method=method.upper(),
                url=url,
                params=params,
                json=json_data,
                data=data,
                files=files,
                headers=req_headers,
                timeout=req_timeout
            )
        except requests.exceptions.Timeout as e:
            raise TimeoutError() from e
        except (requests.exceptions.ConnectionError, requests.exceptions.RequestException) as e:
            raise BackendUnavailableError(f"Failed to connect to backend at {url}: {e}") from e

        # 2xx Success Handlers
        if 200 <= resp.status_code < 300:
            if resp.status_code == 204 or not resp.content:
                return {}
            try:
                return resp.json()
            except Exception:
                return resp.text

        # Structured Error Response Parsing
        error_code = "UNKNOWN_ERROR"
        error_msg = f"HTTP {resp.status_code} Error"
        error_details: Dict[str, Any] = {}
        retryable = False

        try:
            body = resp.json()
            if isinstance(body, dict) and "error" in body and isinstance(body["error"], dict):
                err = body["error"]
                error_code = err.get("code", error_code)
                error_msg = err.get("message", error_msg)
                error_details = err.get("details", {})
                retryable = err.get("retryable", False)
            elif isinstance(body, dict) and "detail" in body:
                error_msg = str(body["detail"])
        except Exception:
            if resp.text:
                error_msg = resp.text

        # Map to specific typed exceptions
        if resp.status_code == 401:
            raise AuthError(error_msg, code=error_code, status_code=401, details=error_details, retryable=retryable)
        elif resp.status_code == 403:
            raise ForbiddenError(error_msg, code=error_code, status_code=403, details=error_details, retryable=retryable)
        elif resp.status_code == 404:
            raise NotFoundError(error_msg, code=error_code, status_code=404, details=error_details, retryable=retryable)
        elif resp.status_code == 409:
            raise ConflictError(error_msg, code=error_code, status_code=409, details=error_details, retryable=retryable)
        elif resp.status_code == 422:
            raise ValidationError(error_msg, code=error_code, status_code=422, details=error_details, retryable=retryable)
        elif resp.status_code == 503:
            raise ServiceUnavailableError(error_msg, code=error_code, status_code=503, details=error_details, retryable=retryable)
        else:
            raise ApiError(error_msg, code=error_code, status_code=resp.status_code, details=error_details, retryable=retryable)

    # --- Health & Readiness ---

    def health_check(self) -> Dict[str, Any]:
        return self.request("GET", "/health")

    def readiness_check(self) -> Dict[str, Any]:
        return self.request("GET", "/ready")

    # --- Dashboard Overview & Aggregations ---

    def get_overview(self) -> Dict[str, Any]:
        """GET /api/v1/dashboard/overview"""
        return self.request("GET", "/api/v1/dashboard/overview")

    def get_cash_flow(self, from_date: Optional[str] = None, to_date: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/v1/dashboard/cash-flow"""
        params = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        return self.request("GET", "/api/v1/dashboard/cash-flow", params=params)

    def get_investments(self, from_date: Optional[str] = None, to_date: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/v1/dashboard/investments"""
        params = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        return self.request("GET", "/api/v1/dashboard/investments", params=params)

    def get_account_freshness(self) -> Dict[str, Any]:
        """GET /api/v1/dashboard/account-freshness"""
        return self.request("GET", "/api/v1/dashboard/account-freshness")

    # --- Accounts ---

    def list_accounts(
        self,
        status: Optional[str] = None,
        account_type: Optional[str] = None,
        owner_user_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """GET /api/v1/accounts"""
        params = {}
        if status:
            params["status"] = status
        if account_type:
            params["account_type"] = account_type
        if owner_user_id:
            params["owner_user_id"] = owner_user_id
        return self.request("GET", "/api/v1/accounts", params=params)

    def create_account(
        self,
        name: str,
        institution: str,
        account_type: str,
        currency: str,
        owner_user_id: Optional[str] = None,
        billing_day: Optional[int] = None,
        due_day: Optional[int] = None,
        linked_cash_account_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """POST /api/v1/accounts"""
        payload = {
            "name": name,
            "institution": institution,
            "account_type": account_type,
            "currency": currency,
            "owner_user_id": owner_user_id,
            "billing_day": billing_day,
            "due_day": due_day,
            "linked_cash_account_id": linked_cash_account_id
        }
        return self.request("POST", "/api/v1/accounts", json_data=payload)

    def update_account(self, account_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """PATCH /api/v1/accounts/{account_id}"""
        return self.request("PATCH", f"/api/v1/accounts/{account_id}", json_data=payload)

    def deactivate_account(self, account_id: str) -> Dict[str, Any]:
        """POST /api/v1/accounts/{account_id}/deactivate"""
        return self.request("POST", f"/api/v1/accounts/{account_id}/deactivate")

    def list_account_aliases(self, account_id: str) -> Dict[str, Any]:
        """GET /api/v1/accounts/{account_id}/aliases"""
        return self.request("GET", f"/api/v1/accounts/{account_id}/aliases")

    def create_account_alias(self, account_id: str, alias: str) -> Dict[str, Any]:
        """POST /api/v1/accounts/{account_id}/aliases"""
        return self.request("POST", f"/api/v1/accounts/{account_id}/aliases", json_data={"alias": alias})

    def delete_account_alias(self, account_id: str, alias_id: str) -> Dict[str, Any]:
        """DELETE /api/v1/accounts/{account_id}/aliases/{alias_id}"""
        return self.request("DELETE", f"/api/v1/accounts/{account_id}/aliases/{alias_id}")

    # --- Categories ---

    def list_categories(self, category_type: Optional[str] = None, status: Optional[str] = "active") -> Dict[str, Any]:
        """GET /api/v1/categories"""
        params = {}
        if category_type:
            params["type"] = category_type
        if status:
            params["status"] = status
        return self.request("GET", "/api/v1/categories", params=params)

    def create_category(self, name: str, category_type: str) -> Dict[str, Any]:
        """POST /api/v1/categories"""
        payload = {"name": name, "type": category_type}
        return self.request("POST", "/api/v1/categories", json_data=payload)

    def update_category(self, category_id: str, name: str) -> Dict[str, Any]:
        """PATCH /api/v1/categories/{category_id}"""
        return self.request("PATCH", f"/api/v1/categories/{category_id}", json_data={"name": name})

    def deactivate_category(self, category_id: str) -> Dict[str, Any]:
        """POST /api/v1/categories/{category_id}/deactivate"""
        return self.request("POST", f"/api/v1/categories/{category_id}/deactivate")

    # --- Credit Cards & Installments ---

    def get_credit_card_state(self, account_id: str) -> Dict[str, Any]:
        """GET /api/v1/credit-cards/{account_id}/state"""
        return self.request("GET", f"/api/v1/credit-cards/{account_id}/state")

    def list_installment_plans(self) -> Dict[str, Any]:
        """GET /api/v1/installments"""
        return self.request("GET", "/api/v1/installments")

    def get_installment_plan(self, plan_id: str) -> Dict[str, Any]:
        """GET /api/v1/installments/{plan_id}"""
        return self.request("GET", f"/api/v1/installments/{plan_id}")

    # --- Snapshots & Manual Calibration ---

    def create_account_snapshot(
        self,
        account_id: str,
        balance: Union[str, Decimal, float],
        as_of: Optional[str] = None,
        currency: Optional[str] = None,
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        POST /api/v1/accounts/{account_id}/snapshots
        Authoritative observation timestamp requires timezone-aware ISO string.
        """
        iso_as_of = as_of or format_iso_timestamp()
        payload: Dict[str, Any] = {
            "balance": f"{Decimal(str(balance)):.2f}",
            "as_of": iso_as_of,
            "source": "dashboard_manual"
        }
        if currency:
            payload["currency"] = currency
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key

        return self.request("POST", f"/api/v1/accounts/{account_id}/snapshots", json_data=payload)

    def create_investment_snapshot(
        self,
        account_id: str,
        total_asset_value: Union[str, Decimal, float],
        currency: str,
        as_of: Optional[str] = None,
        source: str = "dashboard_manual",
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        POST /api/v1/investment-accounts/{account_id}/snapshots
        Conforms strictly to backend contract: total_asset_value, currency, as_of (timezone-aware), source.
        """
        iso_as_of = as_of or format_iso_timestamp()
        payload: Dict[str, Any] = {
            "total_asset_value": f"{Decimal(str(total_asset_value)):.2f}",
            "currency": currency,
            "as_of": iso_as_of,
            "source": source
        }
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key

        return self.request("POST", f"/api/v1/investment-accounts/{account_id}/snapshots", json_data=payload)

    def get_investment_performance(
        self,
        account_id: str,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None
    ) -> Dict[str, Any]:
        """GET /api/v1/investment-accounts/{account_id}/performance"""
        params = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        return self.request("GET", f"/api/v1/investment-accounts/{account_id}/performance", params=params)

    # --- Statements & Reconciliation Review ---

    def upload_statement(
        self,
        account_id: str,
        file_bytes: bytes,
        filename: str = "statement.pdf",
        password: Optional[str] = None
    ) -> Dict[str, Any]:
        """POST /api/v1/accounts/{account_id}/statements (Multipart)"""
        files = {"file": (filename, file_bytes, "application/pdf")}
        data = {}
        if password:
            data["password"] = password
        return self.request("POST", f"/api/v1/accounts/{account_id}/statements", files=files, data=data)

    def get_reconciliation_batch(self, batch_id: str) -> Dict[str, Any]:
        """GET /api/v1/reconciliation-batches/{batch_id}"""
        return self.request("GET", f"/api/v1/reconciliation-batches/{batch_id}")

    def get_reconciliation_preview(self, batch_id: str) -> Dict[str, Any]:
        """GET /api/v1/reconciliation-batches/{batch_id}/preview"""
        return self.request("GET", f"/api/v1/reconciliation-batches/{batch_id}/preview")

    def get_statement_lines(
        self,
        batch_id: str,
        match_status: Optional[str] = None,
        line_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """GET /api/v1/reconciliation-batches/{batch_id}/statement-lines"""
        params = {}
        if match_status:
            params["match_status"] = match_status
        if line_type:
            params["line_type"] = line_type
        return self.request("GET", f"/api/v1/reconciliation-batches/{batch_id}/statement-lines", params=params)

    def commit_reconciliation_batch(self, batch_id: str, row_version: Optional[int] = None) -> Dict[str, Any]:
        """POST /api/v1/reconciliation-batches/{batch_id}/commit"""
        payload = {}
        if row_version is not None:
            payload["row_version"] = row_version
        return self.request("POST", f"/api/v1/reconciliation-batches/{batch_id}/commit", json_data=payload)

    def accept_reconciliation_candidate(
        self,
        candidate_id: str,
        target_transaction_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """POST /api/v1/reconciliation-candidates/{candidate_id}/accept"""
        payload = {}
        if target_transaction_id:
            payload["target_transaction_id"] = target_transaction_id
        return self.request("POST", f"/api/v1/reconciliation-candidates/{candidate_id}/accept", json_data=payload)

    def patch_reconciliation_candidate(self, candidate_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """PATCH /api/v1/reconciliation-candidates/{candidate_id}"""
        return self.request("PATCH", f"/api/v1/reconciliation-candidates/{candidate_id}", json_data={"payload": payload})

    def reject_reconciliation_candidate(self, candidate_id: str, reason: Optional[str] = None) -> Dict[str, Any]:
        """POST /api/v1/reconciliation-candidates/{candidate_id}/reject"""
        payload = {}
        if reason:
            payload["reason"] = reason
        return self.request("POST", f"/api/v1/reconciliation-candidates/{candidate_id}/reject", json_data=payload)

    # --- Work Queue ---

    def get_work_queue(self, type_filter: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/v1/work-queue"""
        params = {}
        if type_filter:
            params["type"] = type_filter
        return self.request("GET", "/api/v1/work-queue", params=params)

    # --- Ingestion Confirmation & Revision ---

    def get_ingestion_request(self, idempotency_key: str) -> Dict[str, Any]:
        """GET /api/v1/ingestion-requests/by-key/{idempotency_key}"""
        return self.request("GET", f"/api/v1/ingestion-requests/by-key/{idempotency_key}")

    def confirm_ingestion_request(self, request_id: str) -> Dict[str, Any]:
        """POST /api/v1/ingestion-requests/{request_id}/confirm"""
        return self.request("POST", f"/api/v1/ingestion-requests/{request_id}/confirm")

    def revise_ingestion_request(self, request_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /api/v1/ingestion-requests/{request_id}/revise"""
        return self.request("POST", f"/api/v1/ingestion-requests/{request_id}/revise", json_data=payload)

    def reject_ingestion_request(self, request_id: str, reason: Optional[str] = None) -> Dict[str, Any]:
        """POST /api/v1/ingestion-requests/{request_id}/reject"""
        payload = {}
        if reason:
            payload["reason"] = reason
        return self.request("POST", f"/api/v1/ingestion-requests/{request_id}/reject", json_data=payload)

    # --- Transactions & History ---

    def list_transactions(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        account_id: Optional[str] = None,
        transaction_type: Optional[str] = None,
        category_id: Optional[str] = None,
        currency: Optional[str] = None,
        verification_status: Optional[str] = None,
        limit: int = 50,
        cursor: Optional[str] = None
    ) -> Dict[str, Any]:
        """GET /api/v1/transactions"""
        params: Dict[str, Any] = {"limit": limit}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if account_id:
            params["account_id"] = account_id
        if transaction_type:
            params["transaction_type"] = transaction_type
        if category_id:
            params["category_id"] = category_id
        if currency:
            params["currency"] = currency
        if verification_status:
            params["verification_status"] = verification_status
        if cursor:
            params["cursor"] = cursor
        return self.request("GET", "/api/v1/transactions", params=params)

    def get_transaction(self, transaction_id: str) -> Dict[str, Any]:
        """GET /api/v1/transactions/{transaction_id}"""
        return self.request("GET", f"/api/v1/transactions/{transaction_id}")

    def preview_transaction_correction(self, transaction_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /api/v1/transactions/{transaction_id}/corrections/preview"""
        return self.request("POST", f"/api/v1/transactions/{transaction_id}/corrections/preview", json_data=payload)

    def commit_transaction_correction(
        self,
        transaction_id: str,
        expected_version: int,
        changes: Dict[str, Any],
        reason: Optional[str] = None
    ) -> Dict[str, Any]:
        """POST /api/v1/transactions/{transaction_id}/corrections/commit"""
        payload = {
            "expected_version": expected_version,
            "changes": changes,
            "reason": reason
        }
        return self.request("POST", f"/api/v1/transactions/{transaction_id}/corrections/commit", json_data=payload)

    def void_transaction(
        self,
        transaction_id: str,
        delete_reason: str,
        expected_version: int
    ) -> Dict[str, Any]:
        """POST /api/v1/transactions/{transaction_id}/void (expected_version is required)"""
        payload = {
            "delete_reason": delete_reason,
            "expected_version": expected_version
        }
        return self.request("POST", f"/api/v1/transactions/{transaction_id}/void", json_data=payload)

    def refund_transaction(
        self,
        transaction_id: str,
        amount: Union[str, Decimal, float],
        currency: str,
        to_account_id: str,
        occurred_on: str,
        remarks: Optional[str] = None
    ) -> Dict[str, Any]:
        """POST /api/v1/transactions/{transaction_id}/refunds"""
        payload = {
            "amount": f"{Decimal(str(amount)):.2f}",
            "currency": currency,
            "to_account_id": to_account_id,
            "occurred_on": occurred_on,
            "remarks": remarks
        }
        return self.request("POST", f"/api/v1/transactions/{transaction_id}/refunds", json_data=payload)

    # --- Audit Events ---

    def list_audit_events(
        self,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        actor_user_id: Optional[str] = None,
        limit: int = 50,
        cursor: Optional[str] = None
    ) -> Dict[str, Any]:
        """GET /api/v1/audit-events"""
        params: Dict[str, Any] = {"limit": limit}
        if entity_type:
            params["entity_type"] = entity_type
        if entity_id:
            params["entity_id"] = entity_id
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if actor_user_id:
            params["actor_user_id"] = actor_user_id
        if cursor:
            params["cursor"] = cursor
        return self.request("GET", "/api/v1/audit-events", params=params)

    # --- Devices Management ---

    def list_devices(self) -> Dict[str, Any]:
        """GET /api/v1/devices"""
        return self.request("GET", "/api/v1/devices")

    def provision_device(self, device_name: str, platform: str, client_version: Optional[str] = None) -> Dict[str, Any]:
        """POST /api/v1/devices"""
        payload = {
            "device_name": device_name,
            "platform": platform,
            "client_version": client_version
        }
        return self.request("POST", "/api/v1/devices", json_data=payload)

    def revoke_device(self, device_id: str) -> Dict[str, Any]:
        """POST /api/v1/devices/{device_id}/revoke"""
        return self.request("POST", f"/api/v1/devices/{device_id}/revoke")
