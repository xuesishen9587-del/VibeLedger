import os
from app import config

os.environ["ENVIRONMENT"] = "test"
os.environ.setdefault("DB_SCHEMA", "vibeledger_test_runner")
if "DATABASE_URL" not in os.environ:
    os.environ["DATABASE_URL"] = "postgresql://postgres:postgres@localhost:5432/postgres"

config.settings = config.Settings()
