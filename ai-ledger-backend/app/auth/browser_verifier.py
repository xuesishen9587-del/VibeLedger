from typing import Protocol, Dict, Any, Optional, List, runtime_checkable
import jwt

from app.domain.auth import InvalidCredentialsError


@runtime_checkable
class BrowserAuthVerifier(Protocol):
    """Protocol for validating browser JWT tokens and extracting verified claims."""
    def verify(self, token: str) -> Dict[str, Any]:
        """
        Validates the token signature and claims.
        Returns the decoded claims dictionary (must include 'sub').
        Raises InvalidCredentialsError if invalid or expired.
        """
        ...


class JWTBrowserAuthVerifier:
    """Production JWT verifier using PyJWT with cryptographic signature verification."""
    def __init__(
        self,
        key: Optional[str] = None,
        algorithms: Optional[List[str]] = None,
        issuer: Optional[str] = None,
        audience: Optional[str] = None,
    ):
        self.key = key
        self.algorithms = algorithms or ["RS256", "HS256"]
        self.issuer = issuer
        self.audience = audience

    def verify(self, token: str) -> Dict[str, Any]:
        if not self.key:
            raise InvalidCredentialsError("JWT verification key is not configured.")

        try:
            options = {
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iss": bool(self.issuer),
                "verify_aud": bool(self.audience),
                "require": ["sub", "exp"],
            }
            claims = jwt.decode(
                token,
                self.key,
                algorithms=self.algorithms,
                issuer=self.issuer,
                audience=self.audience,
                options=options,
            )
        except jwt.ExpiredSignatureError as e:
            raise InvalidCredentialsError("Token has expired.") from e
        except jwt.InvalidIssuerError as e:
            raise InvalidCredentialsError("Token issuer is invalid.") from e
        except jwt.InvalidAudienceError as e:
            raise InvalidCredentialsError("Token audience is invalid.") from e
        except (jwt.InvalidSignatureError, jwt.DecodeError) as e:
            raise InvalidCredentialsError(f"Invalid token signature or encoding: {str(e)}") from e
        except jwt.PyJWTError as e:
            raise InvalidCredentialsError(f"Token validation failed: {str(e)}") from e
        except Exception as e:
            raise InvalidCredentialsError(f"Unexpected error during token verification: {str(e)}") from e

        sub = claims.get("sub")
        if not sub or not isinstance(sub, str) or not sub.strip():
            raise InvalidCredentialsError("Token missing valid 'sub' claim.")

        return claims


class StaticBrowserAuthVerifier:
    """Hermetic static token verifier for testing without network or key dependencies."""
    def __init__(self, token_claims: Optional[Dict[str, Dict[str, Any]]] = None):
        self._token_claims = token_claims or {}

    def register_token(self, token: str, claims: Dict[str, Any]) -> None:
        self._token_claims[token] = claims

    def unregister_token(self, token: str) -> None:
        self._token_claims.pop(token, None)

    def verify(self, token: str) -> Dict[str, Any]:
        if token not in self._token_claims:
            raise InvalidCredentialsError("Invalid or unregistered static test token.")
        claims = self._token_claims[token]
        sub = claims.get("sub")
        if not sub or not isinstance(sub, str) or not sub.strip():
            raise InvalidCredentialsError("Static token missing valid 'sub' claim.")
        return dict(claims)


_active_verifier: Optional[BrowserAuthVerifier] = None

def get_browser_verifier() -> BrowserAuthVerifier:
    """Returns the globally configured or injected BrowserAuthVerifier."""
    global _active_verifier
    if _active_verifier is not None:
        return _active_verifier

    from app.config import get_settings
    settings = get_settings()
    return JWTBrowserAuthVerifier(
        key=settings.AUTH_PUBLIC_KEY,
        algorithms=settings.AUTH_ALGORITHMS,
        issuer=settings.AUTH_ISSUER,
        audience=settings.AUTH_AUDIENCE,
    )

def set_browser_verifier(verifier: Optional[BrowserAuthVerifier]) -> None:
    """Inject a custom BrowserAuthVerifier (e.g. StaticBrowserAuthVerifier during tests)."""
    global _active_verifier
    _active_verifier = verifier
