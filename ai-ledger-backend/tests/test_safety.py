import unittest
from unittest.mock import patch, MagicMock
from app import config

class TestSafety(unittest.TestCase):
    def test_production_environment_rejected(self):
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "production"
        mock_settings.DB_SCHEMA = "vibeledger_target"
        
        with patch("app.config.get_settings", return_value=mock_settings):
            with self.assertRaises(PermissionError):
                config.validate_safety()

    def test_shared_system_schemas_rejected(self):
        for forbidden in ["public", "extensions", "pg_catalog", "information_schema", "vault"]:
            mock_settings = MagicMock()
            mock_settings.ENVIRONMENT = "development"
            mock_settings.DB_SCHEMA = forbidden
            
            with patch("app.config.get_settings", return_value=mock_settings):
                with self.assertRaises(PermissionError):
                    config.validate_safety()
                    
            with self.assertRaises(PermissionError):
                config.validate_schema(forbidden)
                
            with self.assertRaises(PermissionError):
                config.validate_schema(f" {forbidden.upper()} ")

    def test_empty_schema_rejected(self):
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "development"
        mock_settings.DB_SCHEMA = ""
        
        with patch("app.config.get_settings", return_value=mock_settings):
            with self.assertRaises(PermissionError):
                config.validate_safety()
                
        with self.assertRaises(ValueError):
            config.validate_schema("")

    def test_destructive_ops_outside_test_rejected(self):
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "development"
        
        with patch("app.config.get_settings", return_value=mock_settings):
            self.assertFalse(config.is_safe_for_testing())

        mock_settings.ENVIRONMENT = "test"
        with patch("app.config.get_settings", return_value=mock_settings):
            self.assertTrue(config.is_safe_for_testing())

    def test_validate_test_schema_safety(self):
        # 1. Rejected if not test environment
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "development"
        with patch("app.config.get_settings", return_value=mock_settings):
            with self.assertRaises(PermissionError):
                config.validate_test_schema("vibeledger_test_12345")

        # 2. In test environment, reject protected schemas
        mock_settings.ENVIRONMENT = "test"
        with patch("app.config.get_settings", return_value=mock_settings):
            for protected in ["public", "vibeledger_target", "extensions", "pg_catalog", "information_schema"]:
                with self.assertRaises(PermissionError):
                    config.validate_test_schema(protected)
                with self.assertRaises(PermissionError):
                    config.validate_test_schema(f" {protected} ")

            # 3. Reject invalid pattern / arbitrary schemas
            for invalid in ["my_test_schema", "test_schema", "vibeledger", "vibeledger_test", "vibeledger_test-123"]:
                with self.assertRaises(PermissionError):
                    config.validate_test_schema(invalid)

            # 4. Valid test schema accepted
            config.validate_test_schema("vibeledger_test_a1b2c3d4")
            config.validate_test_schema("vibeledger_test_12345678_uuid")
