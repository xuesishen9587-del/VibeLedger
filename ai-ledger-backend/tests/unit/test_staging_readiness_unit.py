import os
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test_user:test_pass@127.0.0.1:5432/vibeledger_test")
os.environ.setdefault("DB_SCHEMA", "vibeledger_test_runner")

import unittest
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
        secret = "super-secret-staging-signing-key-minimum-32-characters"
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

    def test_generate_staging_jwt_secret_length_validation(self):
        # Must fail when secret is shorter than 32 characters
        with self.assertRaises(ValueError) as ctx:
            generate_staging_jwt(secret_key="too-short-secret", sub="owner")
        self.assertIn("at least 32 characters", str(ctx.exception))

        with self.assertRaises(ValueError):
            generate_staging_jwt(secret_key="", sub="owner")
        with self.assertRaises(ValueError):
            generate_staging_jwt(secret_key="a" * 32, sub="")

    def test_staging_cli_safety_refuses_non_staging_environment(self):
        dev_settings = Settings(
            ENVIRONMENT="development",
            DATABASE_URL="postgresql://user:pass@localhost:5432/vibeledger",
            DB_SCHEMA="vibeledger_dev"
        )
        with patch("scripts.generate_staging_browser_token.get_settings", return_value=dev_settings), patch("sys.argv", ["script_name"]):
            with self.assertRaises(SystemExit) as ctx1:
                run_token_generator_cli()
            self.assertEqual(ctx1.exception.code, 1)

        with patch("scripts.bootstrap_staging.get_settings", return_value=dev_settings), patch("sys.argv", ["script_name"]):
            with self.assertRaises(SystemExit) as ctx2:
                run_bootstrap_cli()
            self.assertEqual(ctx2.exception.code, 1)

    def test_staging_cli_reads_from_env_only(self):
        staging_settings = Settings(
            ENVIRONMENT="staging",
            DATABASE_URL="postgresql://user:pass@localhost:5432/vibeledger",
            DB_SCHEMA="vibeledger_staging",
            AUTH_PUBLIC_KEY="a" * 32,
            AUTH_ISSUER="vibeledger-staging",
            AUTH_AUDIENCE="vibeledger-api"
        )
        # Token generator fails if STAGING_OWNER_AUTH_SUBJECT missing
        env_without_sub = dict(os.environ)
        env_without_sub.pop("STAGING_OWNER_AUTH_SUBJECT", None)
        with patch("scripts.generate_staging_browser_token.get_settings", return_value=staging_settings), \
             patch("sys.argv", ["script_name"]), \
             patch.dict(os.environ, env_without_sub, clear=True):
            with self.assertRaises(SystemExit) as ctx1:
                run_token_generator_cli()
            self.assertEqual(ctx1.exception.code, 1)

        # Token generator succeeds when env var is present
        env_with_sub = dict(os.environ)
        env_with_sub["STAGING_OWNER_AUTH_SUBJECT"] = "staging_user_sub"
        with patch("scripts.generate_staging_browser_token.get_settings", return_value=staging_settings), \
             patch("sys.argv", ["script_name"]), \
             patch.dict(os.environ, env_with_sub, clear=True):
            # Must run without error
            run_token_generator_cli()


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
