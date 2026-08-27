import os
import unittest
from pathlib import Path


class TestArchitectureRegression(unittest.TestCase):
    """
    Static Architecture Regression Test for Phase 11.
    Guarantees Dashboard has ZERO direct database access, ZERO SQL queries,
    and consumes Backend REST APIs exclusively.
    """

    def setUp(self):
        self.dashboard_dir = Path(__file__).resolve().parent.parent

    def test_database_module_does_not_exist(self):
        """Ensure legacy database.py is permanently removed."""
        db_file = self.dashboard_dir / "database.py"
        self.assertFalse(
            db_file.exists(),
            f"Forbidden legacy file '{db_file}' still exists. Dashboard must not have direct DB access."
        )

    def test_requirements_has_no_psycopg2(self):
        """Ensure psycopg2 and psycopg2-binary are removed from requirements.txt."""
        req_file = self.dashboard_dir / "requirements.txt"
        self.assertTrue(req_file.exists(), "requirements.txt must exist.")
        content = req_file.read_text(encoding="utf-8")
        self.assertNotIn("psycopg2", content)
        self.assertNotIn("psycopg2-binary", content)
        self.assertIn("requests", content)

    def test_no_database_imports_or_credentials_in_python_files(self):
        """Scan all dashboard python source files to ensure no forbidden DB tokens exist."""
        forbidden_tokens = [
            "psycopg2",
            "DATABASE_URL",
            "DB_SCHEMA",
            "TABLE_SUFFIX",
            "get_db_connection",
            "apply_adjustment",
            "get_credit_card_statement_info"
        ]

        py_files = [
            p for p in self.dashboard_dir.glob("*.py")
            if p.name != "__init__.py"
        ]

        for py_file in py_files:
            text = py_file.read_text(encoding="utf-8")
            for token in forbidden_tokens:
                self.assertNotIn(
                    token,
                    text,
                    f"Forbidden database token '{token}' found in {py_file.name}."
                )

    def test_app_uses_api_client(self):
        """Ensure app.py imports and uses ApiClient."""
        app_file = self.dashboard_dir / "app.py"
        self.assertTrue(app_file.exists(), "app.py must exist.")
        text = app_file.read_text(encoding="utf-8")
        self.assertIn("from api_client import", text)
        self.assertIn("ApiClient", text)
