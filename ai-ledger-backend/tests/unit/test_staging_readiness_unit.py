import unittest
import os
from unittest.mock import patch, MagicMock
from app.config import (
    Settings,
    validate_safety,
    validate_schema,
    is_safe_for_testing,
    validate_test_schema,
    FORBIDDEN_TARGET_SCHEMAS
)
from app.auth.browser_verifier import JWTBrowserAuthVerifier
from scripts.generate_staging_browser_token import generate_staging_jwt, run_token_generator_cli
from scripts.bootstrap_staging import run_bootstrap_cli


class TestStagingReadinessUnit(unittest.TestCase):
    """
    Unit tests for Phase 11.5 staging configuration, HS256 auth, CLI safety guards, and runtime entrypoint.
    """

    def test_settings_environment_staging_accepted(self):
        s = Settings(
            ENVIRONMENT="staging",
            DATABASE_URL="postgresql://user:pass@localhost:5432/vibeledger",
            DB_SCHEMA="vibeledger_staging",
            AUTH_ALGORITHMS=["HS256"],
            AUTH_PUBLIC_KEY="test-staging-secret-key-32-chars-long"
        )
        self.assertEqual(s.ENVIRONMENT, "staging")
        self.assertEqual(s.DB_SCHEMA, "vibeledger_staging")

    def test_validate_safety_permits_staging_and_rejects_production(self):
        staging_settings = Settings(
            ENVIRONMENT="staging",
            DATABASE_URL="postgresql://user:pass@localhost:5432/vibeledger",
            DB_SCHEMA="vibeledger_staging"
        )
        with patch("app.config.get_settings", return_value=staging_settings):
            # Must not raise
            validate_safety()

        prod_settings = Settings(
            ENVIRONMENT="production",
            DATABASE_URL="postgresql://user:pass@localhost:5432/vibeledger",
            DB_SCHEMA="vibeledger_target"
        )
        with patch("app.config.get_settings", return_value=prod_settings):
            with self.assertRaises(PermissionError) as ctx:
                validate_safety()
            self.assertIn("Safety violation", str(ctx.exception))

    def test_destructive_test_guards_strictly_reject_staging(self):
        staging_settings = Settings(
            ENVIRONMENT="staging",
            DATABASE_URL="postgresql://user:pass@localhost:5432/vibeledger",
            DB_SCHEMA="vibeledger_staging"
        )
        with patch("app.config.get_settings", return_value=staging_settings):
            self.assertFalse(is_safe_for_testing())
            with self.assertRaises(PermissionError):
                validate_test_schema("vibeledger_test_123")

    def test_generate_staging_jwt_and_verify_with_jwt_verifier(self):
        secret = "super-secret-staging-signing-key-minimum-32"
        owner_sub = "staging-owner-auth-subject-12345"
        iss = "vibeledger-staging"
        aud = "vibeledger-api"

        token = generate_staging_jwt(
            secret_key=secret,
            sub=owner_sub,
            exp_hours=24,
            issuer=iss,
            audience=aud
        )
        self.assertIsInstance(token, str)
        self.assertTrue(len(token) > 20)

        verifier = JWTBrowserAuthVerifier(
            key=secret,
            algorithms=["HS256"],
            issuer=iss,
            audience=aud
        )
        claims = verifier.verify(token)
        self.assertEqual(claims["sub"], owner_sub)
        self.assertEqual(claims["iss"], iss)
        self.assertEqual(claims["aud"], aud)
        self.assertIn("exp", claims)
        self.assertIn("iat", claims)

    def test_generate_staging_jwt_empty_inputs_fail(self):
        with self.assertRaises(ValueError):
            generate_staging_jwt(secret_key="", sub="owner")
        with self.assertRaises(ValueError):
            generate_staging_jwt(secret_key="secret", sub="")

    def test_staging_cli_safety_refuses_non_staging_environment(self):
        dev_settings = Settings(
            ENVIRONMENT="development",
            DATABASE_URL="postgresql://user:pass@localhost:5432/vibeledger",
            DB_SCHEMA="vibeledger_dev"
        )
        with patch("app.config.get_settings", return_value=dev_settings), patch("sys.argv", ["script_name"]):
            with self.assertRaises(SystemExit) as ctx1:
                run_token_generator_cli()
            self.assertEqual(ctx1.exception.code, 1)

            with self.assertRaises(SystemExit) as ctx2:
                run_bootstrap_cli()
            self.assertEqual(ctx2.exception.code, 1)

    def test_target_app_entrypoint_and_routes(self):
        from app.main import app
        self.assertIsNotNone(app)
        self.assertEqual(app.title, "VibeLedger API")

        # Verify key health & target routes are mounted
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        self.assertIn("/health", routes)
        self.assertIn("/ready", routes)



if __name__ == "__main__":
    unittest.main()
