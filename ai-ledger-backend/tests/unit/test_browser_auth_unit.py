import time
import unittest
from uuid import uuid4
from dataclasses import FrozenInstanceError
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

from app.auth.context import AuthContext
from app.auth.browser_verifier import (
    JWTBrowserAuthVerifier,
    StaticBrowserAuthVerifier,
)
from app.domain.auth import InvalidCredentialsError


class TestBrowserAuthUnit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Generate an RSA keypair for testing RS256 verification
        cls.rsa_private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048
        )
        cls.rsa_public_pem = cls.rsa_private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode("utf-8")
        cls.rsa_private_pem = cls.rsa_private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        ).decode("utf-8")
        cls.hmac_secret = "super-secret-hmac-test-key-32bytes!!"

    def test_jwt_verifier_valid_rs256_token(self):
        verifier = JWTBrowserAuthVerifier(
            key=self.rsa_public_pem,
            algorithms=["RS256"],
            issuer="https://auth.vibeledger.com",
            audience="vibeledger-api"
        )
        now = int(time.time())
        token = jwt.encode(
            {
                "sub": "auth0|12345678",
                "iss": "https://auth.vibeledger.com",
                "aud": "vibeledger-api",
                "exp": now + 3600,
                "nbf": now - 10,
                "email": "user@example.com"
            },
            self.rsa_private_pem,
            algorithm="RS256"
        )

        claims = verifier.verify(token)
        self.assertEqual(claims["sub"], "auth0|12345678")
        self.assertEqual(claims["email"], "user@example.com")

    def test_jwt_verifier_valid_hs256_token(self):
        verifier = JWTBrowserAuthVerifier(
            key=self.hmac_secret,
            algorithms=["HS256"],
            issuer="https://auth.vibeledger.com",
            audience="vibeledger-api"
        )
        now = int(time.time())
        token = jwt.encode(
            {
                "sub": "supabase|user_999",
                "iss": "https://auth.vibeledger.com",
                "aud": "vibeledger-api",
                "exp": now + 3600,
                "nbf": now - 10,
            },
            self.hmac_secret,
            algorithm="HS256"
        )

        claims = verifier.verify(token)
        self.assertEqual(claims["sub"], "supabase|user_999")

    def test_jwt_verifier_expired_token(self):
        verifier = JWTBrowserAuthVerifier(
            key=self.hmac_secret,
            algorithms=["HS256"],
        )
        now = int(time.time())
        token = jwt.encode(
            {
                "sub": "auth0|expired_user",
                "exp": now - 100,  # Expired
            },
            self.hmac_secret,
            algorithm="HS256"
        )

        with self.assertRaises(InvalidCredentialsError) as ctx:
            verifier.verify(token)
        self.assertEqual(str(ctx.exception), "Browser token has expired.")

    def test_jwt_verifier_invalid_issuer(self):
        verifier = JWTBrowserAuthVerifier(
            key=self.hmac_secret,
            algorithms=["HS256"],
            issuer="https://expected.issuer.com"
        )
        now = int(time.time())
        token = jwt.encode(
            {
                "sub": "auth0|bad_issuer",
                "iss": "https://wrong.issuer.com",
                "exp": now + 3600,
            },
            self.hmac_secret,
            algorithm="HS256"
        )

        with self.assertRaises(InvalidCredentialsError) as ctx:
            verifier.verify(token)
        self.assertEqual(str(ctx.exception), "Browser token issuer is invalid.")

    def test_jwt_verifier_invalid_audience(self):
        verifier = JWTBrowserAuthVerifier(
            key=self.hmac_secret,
            algorithms=["HS256"],
            audience="expected-aud"
        )
        now = int(time.time())
        token = jwt.encode(
            {
                "sub": "auth0|bad_aud",
                "aud": "wrong-aud",
                "exp": now + 3600,
            },
            self.hmac_secret,
            algorithm="HS256"
        )

        with self.assertRaises(InvalidCredentialsError) as ctx:
            verifier.verify(token)
        self.assertEqual(str(ctx.exception), "Browser token audience is invalid.")

    def test_jwt_verifier_future_nbf(self):
        verifier = JWTBrowserAuthVerifier(
            key=self.hmac_secret,
            algorithms=["HS256"],
        )
        now = int(time.time())
        token = jwt.encode(
            {
                "sub": "auth0|future_user",
                "exp": now + 3600,
                "nbf": now + 1000,
            },
            self.hmac_secret,
            algorithm="HS256"
        )

        with self.assertRaises(InvalidCredentialsError) as ctx:
            verifier.verify(token)
        self.assertEqual(str(ctx.exception), "Browser token is not yet valid.")

    def test_jwt_verifier_invalid_signature(self):
        verifier = JWTBrowserAuthVerifier(
            key=self.hmac_secret,
            algorithms=["HS256"],
        )
        now = int(time.time())
        token = jwt.encode(
            {
                "sub": "auth0|tampered",
                "exp": now + 3600,
            },
            "different-signing-key-32-bytes-minimum-length!!",
            algorithm="HS256"
        )

        with self.assertRaises(InvalidCredentialsError) as ctx:
            verifier.verify(token)
        self.assertEqual(str(ctx.exception), "Invalid browser credentials.")

    def test_jwt_verifier_missing_sub_claim(self):
        verifier = JWTBrowserAuthVerifier(
            key=self.hmac_secret,
            algorithms=["HS256"],
        )
        now = int(time.time())
        token = jwt.encode(
            {
                "exp": now + 3600,
                # No sub claim
            },
            self.hmac_secret,
            algorithm="HS256"
        )

        with self.assertRaises(InvalidCredentialsError) as ctx:
            verifier.verify(token)
        self.assertEqual(str(ctx.exception), "Invalid browser credentials.")

    def test_jwt_verifier_empty_sub_claim(self):
        verifier = JWTBrowserAuthVerifier(
            key=self.hmac_secret,
            algorithms=["HS256"],
        )
        now = int(time.time())
        token = jwt.encode(
            {
                "sub": "   ",
                "exp": now + 3600,
            },
            self.hmac_secret,
            algorithm="HS256"
        )

        with self.assertRaises(InvalidCredentialsError) as ctx:
            verifier.verify(token)
        self.assertEqual(str(ctx.exception), "Invalid browser credentials.")

    def test_jwt_verifier_alg_none_rejected(self):
        verifier = JWTBrowserAuthVerifier(
            key=self.hmac_secret,
            algorithms=["HS256"],
        )
        # Unsigned/alg=none token format: header.payload.
        token = "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJzdWIiOiJ1c2VyMSIsImV4cCI6OTk5OTk5OTk5OX0."
        with self.assertRaises(InvalidCredentialsError) as ctx:
            verifier.verify(token)
        self.assertEqual(str(ctx.exception), "Invalid browser credentials.")

    def test_jwt_verifier_malformed_3_segment(self):
        verifier = JWTBrowserAuthVerifier(
            key=self.hmac_secret,
            algorithms=["HS256"],
        )
        token = "not_base64_header.not_base64_payload.not_base64_sig"
        with self.assertRaises(InvalidCredentialsError) as ctx:
            verifier.verify(token)
        self.assertEqual(str(ctx.exception), "Invalid browser credentials.")
        # Ensure token content is never reflected in exception message
        self.assertNotIn("not_base64_header", str(ctx.exception))

    def test_jwt_verifier_empty_key(self):
        verifier = JWTBrowserAuthVerifier(key=None)
        with self.assertRaises(InvalidCredentialsError):
            verifier.verify("some.jwt.token")

    def test_static_verifier(self):
        static_verifier = StaticBrowserAuthVerifier({
            "token_alpha": {"sub": "user_alpha", "email": "alpha@example.com"},
            "token_beta": {"sub": "user_beta"}
        })

        claims = static_verifier.verify("token_alpha")
        self.assertEqual(claims["sub"], "user_alpha")
        self.assertEqual(claims["email"], "alpha@example.com")

        with self.assertRaises(InvalidCredentialsError):
            static_verifier.verify("token_unknown")

        static_verifier.register_token("token_gamma", {"sub": "user_gamma"})
        self.assertEqual(static_verifier.verify("token_gamma")["sub"], "user_gamma")

        static_verifier.unregister_token("token_gamma")
        with self.assertRaises(InvalidCredentialsError):
            static_verifier.verify("token_gamma")

    def test_auth_context_immutability_and_helpers(self):
        uid = uuid4()
        hid = uuid4()
        dev_id = uuid4()

        ctx = AuthContext(
            auth_mode="device",
            user_id=uid,
            household_id=hid,
            household_role="owner",
            device_id=dev_id,
            auth_subject="auth_sub_1"
        )

        self.assertTrue(ctx.is_device)
        self.assertFalse(ctx.is_browser)
        self.assertTrue(ctx.is_owner)
        self.assertTrue(ctx.can_write)

        # Ensure immutability
        with self.assertRaises(FrozenInstanceError):
            ctx.household_role = "viewer"  # type: ignore

        # Test viewer role
        viewer_ctx = AuthContext(
            auth_mode="browser",
            user_id=uid,
            household_id=hid,
            household_role="viewer",
            device_id=None,
            auth_subject="auth_sub_viewer"
        )
        self.assertFalse(viewer_ctx.is_device)
        self.assertTrue(viewer_ctx.is_browser)
        self.assertFalse(viewer_ctx.is_owner)
        self.assertFalse(viewer_ctx.can_write)


if __name__ == "__main__":
    unittest.main()
