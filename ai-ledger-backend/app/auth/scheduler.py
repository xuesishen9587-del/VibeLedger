"""Google OIDC boundary for the single internal daily schedule command."""
from fastapi import Header
from google.auth.transport.requests import Request
from google.oauth2 import id_token
import jwt
import requests

from app.config import get_settings
from app.domain.spending import fail


def verify_scheduler_token(token, audience, service_account):
    # Reject browser/device and malformed tokens before fetching Google certificates.
    # Unverified claims only reject; authorization always requires signature verification.
    try:
        header = jwt.get_unverified_header(token)
        claims = jwt.decode(token, options={"verify_signature": False})
        if (header.get("alg") != "RS256" or not header.get("kid")
                or claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com")
                or claims.get("aud") != audience or claims.get("email") != service_account):
            raise ValueError()
        with requests.Session() as session:
            transport = Request(session=session)
            def bounded_request(url, method="GET", **kwargs):
                kwargs["timeout"] = 5
                return transport(url=url, method=method, **kwargs)
            verified = id_token.verify_oauth2_token(token, bounded_request, audience=audience)
        if (verified.get("aud") != audience or verified.get("email") != service_account
                or verified.get("email_verified") is not True or not verified.get("sub")):
            raise ValueError()
    except Exception:
        fail("INVALID_SCHEDULER_CREDENTIALS", "A valid scheduler service identity is required.", 401)


def require_scheduler(authorization: str | None = Header(None)):
    settings = get_settings()
    if not settings.SCHEDULER_AUDIENCE or not settings.SCHEDULER_SERVICE_ACCOUNT:
        fail("SCHEDULER_NOT_CONFIGURED", "Daily schedule invocation is not configured.", 503)
    if not authorization or not authorization.startswith("Bearer "):
        fail("INVALID_SCHEDULER_CREDENTIALS", "A scheduler service identity is required.", 401)
    verify_scheduler_token(authorization[7:].strip(), settings.SCHEDULER_AUDIENCE,
                           settings.SCHEDULER_SERVICE_ACCOUNT)
