import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ANTHROPIC_API_KEY: str
    SUPABASE_URL: str
    SUPABASE_SERVICE_ROLE_KEY: str
    TWILIO_ACCOUNT_SID: str
    TWILIO_AUTH_TOKEN: str
    TWILIO_PHONE_NUMBER: str

    DEFAULT_TIMEZONE: str = "America/New_York"
    ANTHROPIC_MODEL: str = "claude-haiku-4-5"
    MAX_TOOL_ITERATIONS: int = 5
    SKIP_TWILIO_SIGNATURE_VALIDATION: bool = False


settings = Settings()  # type: ignore[call-arg]
