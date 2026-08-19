import os
from typing import Literal, Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    ENVIRONMENT: Literal["development", "test", "production"] = Field(
        ...,
        description="The running environment. Must be explicitly set to 'development', 'test', or 'production'."
    )
    DATABASE_URL: str = Field(
        ...,
        description="PostgreSQL connection string."
    )
    DB_SCHEMA: str = Field(
        ...,
        description="Target database schema. Must be explicitly set and cannot be 'public'."
    )
    GEMINI_API_KEY: Optional[str] = Field(
        None,
        description="API key for Gemini client."
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
        if val.lower() == "public":
            raise ValueError("DB_SCHEMA cannot be 'public' to protect legacy production tables.")
        return val

# Load active settings
# Note: we catch validation errors and report them clearly to guide setting up env variables.
try:
    settings = Settings()
except Exception as e:
    # During testing we might want to let validation fail if they check it programmatically,
    # but we also need a way to let test suite run its safety assertions.
    # We will instantiate a dummy settings object if env is not fully configured,
    # but raise errors when validate_safety() is called.
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
    if not schema or schema == "public":
        raise PermissionError("Safety violation: Execution schema cannot be empty or 'public'.")

def validate_schema(schema: str) -> None:
    """
    Verifies that the provided schema identifier is safe and explicitly not 'public'.
    """
    s = schema.strip().lower()
    if not s:
        raise ValueError("Schema name cannot be empty.")
    if s == "public":
        raise PermissionError("Schema 'public' is protected and cannot be used for target database migrations or tests.")

def is_safe_for_testing() -> bool:
    """
    Returns True if we are in a safe 'test' environment to allow destructive test schema operations.
    """
    try:
        current_settings = get_settings()
        return current_settings.ENVIRONMENT == "test"
    except Exception:
        return False
