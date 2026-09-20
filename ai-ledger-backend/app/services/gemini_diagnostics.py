"""Allowlisted Gemini diagnostics. Never format an exception or its response body."""
from contextlib import contextmanager
from contextvars import ContextVar
import logging
from uuid import UUID

import httpx


_request_id = ContextVar("gemini_request_id", default=None)
logger = logging.getLogger("app.gemini")


@contextmanager
def request_context(identity):
    token = _request_id.set(str(UUID(str(identity))))
    try:
        yield
    finally:
        _request_id.reset(token)


def log_failure(operation, phase, exc):
    status = getattr(exc, "code", None)
    if type(status) is not int or not 100 <= status <= 599:
        status = None
    timeout = isinstance(exc, (TimeoutError, httpx.TimeoutException)) or status in (408, 504)
    category = "timeout" if timeout else phase
    # No str(exc), traceback, SDK response, headers, prompt or financial fields.
    logger.warning(
        "gemini_failure operation=%s request_id=%s category=%s phase=%s exception_type=%s upstream_status=%s",
        operation, _request_id.get(), category, phase, type(exc).__name__, status,
    )
