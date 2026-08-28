import os
import re
from typing import Literal, Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TEST_SCHEMA_REGEX = re.compile(r"^vibeledger_test_[a-zA-Z0-9_]+$")
PROTECTED_SCHEMAS = {"public", "vibeledger_target", "extensions", "pg_catalog", "information_schema", "vault"}
FORBIDDEN_TARGET_SCHEMAS = {
    "public",
    "extensions",
    "pg_catalog",
    "information_schema",
    "vault",
    "graphql",
    "graphql_public",
    "realtime",
    "storage",
    "auth",
}

class Settings(BaseSettings):
    ENVIRONMENT: Literal["development", "test", "staging", "production"] = Field(
        ...,
        description="The running environment. Must be explicitly set to 'development', 'test', 'staging', or 'production'."
    )
    DATABASE_URL: str = Field(
        ...,
        description="PostgreSQL connection string."
    )
    DB_SCHEMA: str = Field(
        ...,
        description="Target database schema. Must be explicitly set and cannot be a shared/system schema."
    )
    GEMINI_API_KEY: Optional[str] = Field(
        None,
        description="API key for Gemini client."
    )
    MAX_EXPENSE_IMAGE_BYTES: int = Field(
        10 * 1024 * 1024,
        description="Maximum allowed decoded image size in bytes (default: 10MB)."
    )
    MAX_STATEMENT_PDF_BYTES: int = Field(
        20 * 1024 * 1024,
        description="Maximum allowed Statement PDF file size in bytes (default: 20MB)."
    )
    FX_API_BASE_URL: str = Field(
        "https://api.frankfurter.app",
        description="Base URL for public reference FX rates provider."
    )
    FX_HTTP_TIMEOUT_SECONDS: float = Field(
        5.0,
        description="HTTP request timeout for external reference FX provider in seconds."
    )
    AUTH_ISSUER: Optional[str] = Field(
        None,
        description="Expected JWT issuer (iss claim)."
    )
    AUTH_AUDIENCE: Optional[str] = Field(
        None,
        description="Expected JWT audience (aud claim)."
    )
    AUTH_PUBLIC_KEY: Optional[str] = Field(
        None,
        description="Static PEM public key or secret for JWT verification."
    )
    AUTH_ALGORITHMS: list[str] = Field(
        default_factory=lambda: ["RS256", "HS256"],
        description="Permitted JWT signature algorithms."
    )
    AUTH_JWKS_URL: Optional[str] = Field(
        None,
        description="Optional JWKS URL for external identity provider (disabled/mocked in tests)."
    )

    # Use SettingsConfigDict for Pydantic v2 Settings configuration
    model_config = SettingsConfigDict(
        env_file=os.environ.get("ENV_FILE_PATH", ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @field_validator("DB_SCHEMA")
    @classmethod
    def validate_db_schema_name(cls, v: str) -> str:
        val = v.strip()
        if not val:
            raise ValueError("DB_SCHEMA cannot be empty or whitespace.")
        if val.lower() in FORBIDDEN_TARGET_SCHEMAS:
            raise ValueError(f"DB_SCHEMA cannot be '{val}' (shared or system schema).")
        return val

# Load active settings
try:
    settings = Settings()
except Exception as e:
    settings = None
    settings_load_error = e
else:
    settings_load_error = None

def get_settings() -> Settings:
    if settings is None:
        raise ValueError(f"Failed to initialize configuration settings. Details: {settings_load_error}")
    return settings

def validate_safety() -> None:
    """
    Validates that we are not running migrations or tests in a production environment,
    and that the schema is explicitly configured and safe.
    """
    current_settings = get_settings()
    if current_settings.ENVIRONMENT == "production":
        raise PermissionError("Safety violation: Execution is forbidden in production environment.")
    
    schema = current_settings.DB_SCHEMA.strip().lower()
    if not schema or schema in FORBIDDEN_TARGET_SCHEMAS:
        raise PermissionError(f"Safety violation: Execution schema cannot be empty or a shared/system schema ('{schema}').")

def validate_schema(schema: str) -> None:
    """
    Verifies that the provided schema identifier is safe and explicitly not a shared or system schema.
    """
    s = schema.strip().lower()
    if not s:
        raise ValueError("Schema name cannot be empty.")
    if s in FORBIDDEN_TARGET_SCHEMAS:
        raise PermissionError(f"Schema '{schema}' is a shared/system schema and cannot be used for target database migrations or tests.")

def is_safe_for_testing() -> bool:
    """
    Returns True if we are in a safe 'test' environment to allow destructive test schema operations.
    """
    try:
        current_settings = get_settings()
        return current_settings.ENVIRONMENT == "test"
    except Exception:
        return False

def validate_test_schema(schema: str) -> None:
    """
    Validates that a schema is a temporary test schema eligible for destructive DROP SCHEMA.
    Requires ENVIRONMENT == 'test' and schema matching '^vibeledger_test_[a-zA-Z0-9_]+$'.
    Explicitly forbids dropping 'public', 'vibeledger_target', 'extensions', etc.
    """
    if not is_safe_for_testing():
        raise PermissionError("Destructive test schema operations are only allowed when ENVIRONMENT='test'.")
    
    s = schema.strip().lower()
    if s in PROTECTED_SCHEMAS or s in FORBIDDEN_TARGET_SCHEMAS:
        raise PermissionError(f"Safety violation: Schema '{schema}' is protected and can NEVER be dropped by test cleanup.")
        
    if not TEST_SCHEMA_REGEX.match(s):
        raise PermissionError(
            f"Safety violation: Test schema '{schema}' does not match required pattern 'vibeledger_test_<identifier>'. "
            "Destructive DROP SCHEMA is rejected."
        )
