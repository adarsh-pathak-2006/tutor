"""Configuration from environment variables (and the repository's .env locally). Validated once at start-up:
a misconfigured service refuses to start instead of failing on the first request.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AI_DIR = Path(__file__).resolve().parent.parent  # tutor/ai
REPO_ENV = AI_DIR.parent / ".env"  # tutor/.env, shared with Django locally

ProviderName = Literal["mock", "ollama", "gemini", "gateway", "hosted"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ENV, env_file_encoding="utf-8", extra="ignore")

    environment: Literal["local", "test", "hosted", "production"] = Field("local", alias="TUTOR_ENV")
    database_url: SecretStr = Field(alias="AI_DATABASE_URL")
    service_token: SecretStr = Field(alias="TUTOR_AI_SERVICE_TOKEN")
    previous_service_token: SecretStr | None = Field(None, alias="TUTOR_AI_SERVICE_TOKEN_PREVIOUS")

    provider: ProviderName = Field("mock", alias="TUTOR_AI_PROVIDER")
    embedding_dimensions: int = Field(768, alias="TUTOR_AI_EMBEDDING_DIMENSIONS", ge=64, le=4096)

    ollama_url: str = Field("http://localhost:11434", alias="TUTOR_OLLAMA_URL")
    ollama_chat_model: str = Field("llama3.2", alias="TUTOR_OLLAMA_CHAT_MODEL", min_length=1)
    ollama_embed_model: str = Field("nomic-embed-text", alias="TUTOR_OLLAMA_EMBED_MODEL", min_length=1)
    ollama_keep_alive: str = Field("30m", alias="TUTOR_OLLAMA_KEEP_ALIVE")
    ollama_max_batch_size: int = Field(32, alias="TUTOR_OLLAMA_MAX_BATCH_SIZE", ge=1, le=64)
    ollama_timeout_seconds: float = Field(180.0, alias="TUTOR_OLLAMA_TIMEOUT_SECONDS", gt=0, le=900)
    # Gemini chat configuration
    gemini_api_key: SecretStr | None = Field(
        None,
        alias="TUTOR_GEMINI_API_KEY",
    )
    gemini_chat_model: str = Field(
        "gemini-3.5-flash-lite",
        alias="TUTOR_GEMINI_CHAT_MODEL",
        min_length=1,
    )
    # LLM Gateway configuration
    gateway_primary: Literal["mock", "ollama", "gemini"] = Field(
        "gemini",
        alias="TUTOR_GATEWAY_PRIMARY",
    )
    gateway_fallback: Literal["mock", "ollama", "gemini", "none"] = Field(
        "ollama",
        alias="TUTOR_GATEWAY_FALLBACK",
    )
    ollama_connect_timeout: float = Field(15.0, alias="TUTOR_OLLAMA_CONNECT_TIMEOUT", gt=0, le=300)
    query_cache_max_entries: int = Field(2000, alias="TUTOR_AI_QUERY_CACHE_MAX_ENTRIES", ge=0, le=50_000)
    db_pool_size: int = Field(10, alias="TUTOR_AI_DB_POOL_SIZE", ge=1, le=100)
    db_max_overflow: int = Field(15, alias="TUTOR_AI_DB_MAX_OVERFLOW", ge=0, le=100)

    # Tutor prompts and size limits (design §6). Locally they suit a 3B model on a CPU; the hosted provider gets
    # larger limits once it is chosen (D23).
    prompts_dir: Path = Field(AI_DIR / "prompts", alias="TUTOR_AI_PROMPTS_DIR")
    context_tokens: int = Field(600, alias="TUTOR_AI_CONTEXT_TOKENS", ge=100, le=20_000)
    history_turns: int = Field(6, alias="TUTOR_AI_HISTORY_TURNS", ge=0, le=50)
    history_tokens: int = Field(900, alias="TUTOR_AI_HISTORY_TOKENS", ge=0, le=20_000)
    reply_tokens: int = Field(250, alias="TUTOR_AI_REPLY_TOKENS", ge=20, le=4_000)
    retrieval_top_k: int = Field(4, alias="TUTOR_AI_RETRIEVAL_TOP_K", ge=1, le=20)
    # Minimum cosine similarity for a passage to count as relevant. None = the provider's default (retrieval.py);
    # the right value depends on the embedding model and is calibrated with the eval suites.
    min_similarity: float | None = Field(None, alias="TUTOR_AI_MIN_SIMILARITY", ge=0, le=1)
    # Tutor turns (design §3, §7).
    safety_policy: Path = Field(AI_DIR / "safety" / "v1.toml", alias="TUTOR_AI_SAFETY_POLICY")
    turn_timeout_seconds: float = Field(120.0, alias="TUTOR_AI_TURN_TIMEOUT_SECONDS", gt=0, le=900)
    # Replies with less than this share of words from the lesson are flagged "ungrounded" (not replaced: a small
    # local model paraphrases a lot; the action is revisited with evals).
    grounding_min: float = Field(0.2, alias="TUTOR_AI_GROUNDING_MIN", ge=0, le=1)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field("INFO", alias="TUTOR_AI_LOG_LEVEL")
    trusted_proxies: int = Field(0, alias="TUTOR_TRUSTED_PROXIES", ge=0, le=5)

    @field_validator("database_url")
    @classmethod
    def _async_driver(cls, value: SecretStr) -> SecretStr:
        """Accept the usual postgres:// / postgresql:// URLs and use the psycopg 3 async driver."""
        url = value.get_secret_value()
        for prefix in ("postgres://", "postgresql://", "postgresql+psycopg://"):
            if url.startswith(prefix):
                return SecretStr("postgresql+psycopg://" + url[len(prefix) :])
        raise ValueError("AI_DATABASE_URL must be a PostgreSQL URL")

    @field_validator("service_token", "previous_service_token")
    @classmethod
    def _strong_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < 32:
            raise ValueError("service tokens must be at least 32 characters (use a random value)")
        return value

    @model_validator(mode="after")
    def _production_rules(self) -> "Settings":
        if self.environment in ("hosted", "production") and self.provider == "ollama":
            raise ValueError("a hosted service can't use a laptop's Ollama; use mock (demo) or hosted (production)")
        if (
            self.provider == "gemini"
            or (self.provider == "gateway" and (self.gateway_primary == "gemini" or self.gateway_fallback == "gemini"))
        ) and self.gemini_api_key is None:
            raise ValueError("TUTOR_GEMINI_API_KEY is required when Gemini is selected")
        if self.environment == "production" and self.provider == "mock":
            raise ValueError("production must not answer students with the mock provider")
        return self

    @property
    def accepted_tokens(self) -> list[str]:
        tokens = [self.service_token.get_secret_value()]
        if self.previous_service_token is not None:
            tokens.append(self.previous_service_token.get_secret_value())
        return tokens


@lru_cache
def get_settings() -> Settings:
    return Settings()  # values come from the environment (validated at start-up)
