import os

# Safe dummy test config for hermetic unit test bootstrap (0 DB connections, no .env dependency)
os.environ["ENVIRONMENT"] = "test"
os.environ.setdefault("DATABASE_URL", "postgresql://test_user:test_pass@127.0.0.1:5432/vibeledger_test")
os.environ.setdefault("DB_SCHEMA", "vibeledger_test_runner")

from app import config

config.settings = config.Settings()
