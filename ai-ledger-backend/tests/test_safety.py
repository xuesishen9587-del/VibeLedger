import unittest
from unittest.mock import patch, MagicMock
from app import config

class TestSafety(unittest.TestCase):
    def test_production_environment_rejected(self):
        # When ENVIRONMENT=production, validate_safety must raise PermissionError
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "production"
        mock_settings.DB_SCHEMA = "vibeledger_target"
        
        with patch("app.config.get_settings", return_value=mock_settings):
            with self.assertRaises(PermissionError):
                config.validate_safety()

    def test_public_schema_rejected(self):
        # When DB_SCHEMA=public, validate_safety and validate_schema must raise error
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "development"
        mock_settings.DB_SCHEMA = "public"
        
        with patch("app.config.get_settings", return_value=mock_settings):
            with self.assertRaises(PermissionError):
                config.validate_safety()
                
        with self.assertRaises(PermissionError):
            config.validate_schema("public")
            
        with self.assertRaises(PermissionError):
            config.validate_schema(" PUBLIC ")

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
        # is_safe_for_testing should be False if ENVIRONMENT != test
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "development"
        
        with patch("app.config.get_settings", return_value=mock_settings):
            self.assertFalse(config.is_safe_for_testing())

        mock_settings.ENVIRONMENT = "test"
        with patch("app.config.get_settings", return_value=mock_settings):
            self.assertTrue(config.is_safe_for_testing())
