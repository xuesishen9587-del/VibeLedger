import os
from app import config

os.environ["ENVIRONMENT"] = "test"
os.environ["DB_SCHEMA"] = "vibeledger_test_runner"
config.settings = config.Settings()
