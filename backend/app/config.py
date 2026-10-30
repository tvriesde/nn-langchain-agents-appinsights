from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    cosmos_endpoint: str
    cosmos_database: str = "recipes"
    cosmos_container: str = "conversations"
    azure_openai_endpoint: str
    azure_openai_deployment: str = "gpt-6-luna"
    azure_openai_api_version: str = "2024-10-21"
    azure_client_id: str | None = None
    applicationinsights_connection_string: str | None = None
    capture_message_content: bool = True
    cookie_secure: bool = True
    max_input_chars: int = Field(default=4000, ge=1, le=16000)
    max_context_turns: int = Field(default=10, ge=1, le=50)
    max_output_tokens: int = Field(default=1800, ge=100, le=4000)
    turn_timeout_seconds: int = Field(default=180, ge=10, le=220)
    requests_per_minute: int = Field(default=6, ge=1, le=60)
    static_dir: Path = Path(__file__).resolve().parents[1] / "static"
