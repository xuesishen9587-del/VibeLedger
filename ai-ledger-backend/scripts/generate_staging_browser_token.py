import os
import sys
import argparse
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
import jwt

# Ensure project root is on sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.config import get_settings


def generate_staging_jwt(
    secret_key: str,
    sub: str,
    exp_hours: int = 168,  # Default 7 days
    issuer: Optional[str] = None,
    audience: Optional[str] = None,
    extra_claims: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Generates a valid PyJWT signed Browser JWT for staging access using HS256 HMAC signing.
    Embeds required standard claims (sub, iat, exp) and optional iss/aud.
    Enforces minimum 32-character high-entropy secret.
    """
    if not secret_key or not secret_key.strip():
        raise ValueError("Signing secret_key cannot be empty.")
    if len(secret_key.strip()) < 32:
        raise ValueError("Staging HS256 shared secret (AUTH_PUBLIC_KEY) must be at least 32 characters long for cryptographic safety.")
    if not sub or not sub.strip():
        raise ValueError("Subject (sub) cannot be empty.")

    now = datetime.now(timezone.utc)
    exp = now + timedelta(hours=exp_hours)

    payload: Dict[str, Any] = {
        "sub": sub.strip(),
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }

    if issuer and issuer.strip():
        payload["iss"] = issuer.strip()
    if audience and audience.strip():
        payload["aud"] = audience.strip()

    if extra_claims:
        for k, v in extra_claims.items():
            if k not in payload:
                payload[k] = v

    token = jwt.encode(payload, secret_key.strip(), algorithm="HS256")
    return token


def run_token_generator_cli():
    parser = argparse.ArgumentParser(
        description="VibeLedger Staging Browser JWT Generator (Staging-only HS256 HMAC signing)"
    )
    parser.add_argument(
        "--exp-hours",
        type=int,
        default=168,
        help="Token expiration lifetime in hours (default: 168 hours / 7 days)."
    )

    args = parser.parse_args()

    # Strict CLI safety check
    settings = get_settings()
    if settings.ENVIRONMENT != "staging":
        print(f"ERROR: Operator CLI execution is strictly restricted to ENVIRONMENT='staging'. Current: '{settings.ENVIRONMENT}'.")
        sys.exit(1)

    # Subject must come from STAGING_OWNER_AUTH_SUBJECT environment variable only
    sub = os.environ.get("STAGING_OWNER_AUTH_SUBJECT")
    if not sub or not sub.strip():
        print("ERROR: Subject must be provided via STAGING_OWNER_AUTH_SUBJECT environment variable.")
        sys.exit(1)

    # Secret must come from AUTH_PUBLIC_KEY environment / settings only
    secret = settings.AUTH_PUBLIC_KEY
    if not secret or not secret.strip():
        print("ERROR: HS256 signing secret must be configured via AUTH_PUBLIC_KEY environment variable.")
        sys.exit(1)

    try:
        token = generate_staging_jwt(
            secret_key=secret,
            sub=sub.strip(),
            exp_hours=args.exp_hours,
            issuer=settings.AUTH_ISSUER,
            audience=settings.AUTH_AUDIENCE,
        )
    except ValueError as ve:
        print(f"ERROR: {ve}")
        sys.exit(1)

    print("================================================================================")
    print("  VIBELEDGER STAGING BROWSER JWT (HS256 HMAC SIGNED — STAGING ONLY)             ")
    print("================================================================================")
    print(f"Subject (sub) : {sub.strip()}")
    print(f"Expires In    : {args.exp_hours} hours")
    if settings.AUTH_ISSUER:
        print(f"Issuer (iss)  : {settings.AUTH_ISSUER}")
    if settings.AUTH_AUDIENCE:
        print(f"Audience (aud): {settings.AUTH_AUDIENCE}")
    print("--------------------------------------------------------------------------------")
    print("Generated Token:")
    print(token)
    print("--------------------------------------------------------------------------------")
    print("Usage: Set AUTH_TOKEN in Dashboard session or pass as 'Bearer <token>' in HTTP headers.")
    print("WARNING: This token is valid only in staging and must NEVER be used in production.")
    print("================================================================================")


if __name__ == "__main__":
    run_token_generator_cli()
