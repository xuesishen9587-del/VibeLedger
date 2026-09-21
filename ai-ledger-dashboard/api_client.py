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
        balance_scope: str,
        account_type: str,
        currency: str,
        idempotency_key: str,
        owner_user_id: Optional[str] = None,
        risk_level: Optional[str] = None,
        opened_on: Optional[str] = None,
        statement_import_enabled: bool = False
    ) -> Dict[str, Any]:
        """POST /api/v1/accounts"""
        if not idempotency_key:
            raise ValueError("idempotency_key is required for create_account")
        payload: Dict[str, Any] = {
            "name": name,
            "balance_scope": balance_scope,
            "account_type": account_type,
            "currency": currency,
            "statement_import_enabled": statement_import_enabled
        }
        if owner_user_id:
            payload["owner_user_id"] = owner_user_id
        if risk_level:
            payload["risk_level"] = risk_level
        if opened_on:
            payload["opened_on"] = opened_on
        headers = {"Idempotency-Key": idempotency_key}
        return self.request("POST", "/api/v1/accounts", json_data=payload, headers=headers)

    def update_account(self, account_id: str, payload: Dict[str, Any], idempotency_key: str) -> Dict[str, Any]:
        """PATCH /api/v1/accounts/{account_id}"""
        if not idempotency_key:
            raise ValueError("idempotency_key is required for update_account")
        headers = {"Idempotency-Key": idempotency_key}
        return self.request("PATCH", f"/api/v1/accounts/{account_id}", json_data=payload, headers=headers)

    def list_account_aliases(self, account_id: str) -> Dict[str, Any]:
        """GET /api/v1/accounts/{account_id}/aliases"""
        return self.request("GET", f"/api/v1/accounts/{account_id}/aliases")

    def create_account_alias(self, account_id: str, alias: str, idempotency_key: str) -> Dict[str, Any]:
        """POST /api/v1/accounts/{account_id}/aliases"""
        if not idempotency_key:
            raise ValueError("idempotency_key is required for create_account_alias")
        headers = {"Idempotency-Key": idempotency_key}
        return self.request("POST", f"/api/v1/accounts/{account_id}/aliases", json_data={"alias": alias}, headers=headers)

    def update_account_alias(
        self, account_id: str, alias_id: str, payload: Dict[str, Any], idempotency_key: str
    ) -> Dict[str, Any]:
        """PATCH /api/v1/accounts/{account_id}/aliases/{alias_id}"""
        if not idempotency_key:
            raise ValueError("idempotency_key is required for update_account_alias")
        headers = {"Idempotency-Key": idempotency_key}
        return self.request("PATCH", f"/api/v1/accounts/{account_id}/aliases/{alias_id}", json_data=payload, headers=headers)

    # --- Categories ---

    def list_categories(self, category_type: Optional[str] = None, status: Optional[str] = "active") -> Dict[str, Any]:
        """GET /api/v1/categories"""
        params = {}
        if category_type:
            params["type"] = category_type
        if status:
            params["status"] = status
        return self.request("GET", "/api/v1/categories", params=params)

    def create_category(
        self, name: str, category_type: str, idempotency_key: str, description: Optional[str] = None
    ) -> Dict[str, Any]:
        """POST /api/v1/categories"""
        if not idempotency_key:
            raise ValueError("idempotency_key is required for create_category")
        payload: Dict[str, Any] = {"name": name, "type": category_type}
        if description:
            payload["description"] = description
        headers = {"Idempotency-Key": idempotency_key}
        return self.request("POST", "/api/v1/categories", json_data=payload, headers=headers)

    def update_category(self, category_id: str, payload: Dict[str, Any], idempotency_key: str) -> Dict[str, Any]:
        """PATCH /api/v1/categories/{category_id}"""
        if not idempotency_key:
            raise ValueError("idempotency_key is required for update_category")
        headers = {"Idempotency-Key": idempotency_key}
        return self.request("PATCH", f"/api/v1/categories/{category_id}", json_data=payload, headers=headers)

    # --- Credit Cards & Installments ---




    # --- Snapshots & Manual Calibration ---




    # --- Statements & Reconciliation Review ---










    # --- Work Queue ---


    # --- Ingestion Confirmation & Revision ---





    # --- Transactions & History ---







    # --- Audit Events ---


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

    def revoke_device(self, device_id: str, idempotency_key: str) -> Dict[str, Any]:
        """POST /api/v1/devices/{device_id}/revoke"""
        if not idempotency_key:
            raise ValueError("idempotency_key is required for revoke_device")
        headers = {"Idempotency-Key": idempotency_key}
        return self.request("POST", f"/api/v1/devices/{device_id}/revoke", headers=headers)
