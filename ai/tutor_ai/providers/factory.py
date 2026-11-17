"""Factory for creating configured AI model providers."""

from ..settings import Settings
from .base import ModelProvider
from .gateway import LLMGatewayProvider
from .gemini import GeminiProvider
from .mock import MockProvider
from .ollama import OllamaProvider


class ProviderNotConfigured(RuntimeError):
    """Raised when the requested provider is not implemented."""


def _build_individual_provider(name: str, settings: Settings) -> ModelProvider:
    """Build an individual underlying model provider by name."""
    if name == "mock":
        return MockProvider(dimensions=settings.embedding_dimensions)

    if name == "ollama":
        return OllamaProvider(
            base_url=settings.ollama_url,
            chat_model=settings.ollama_chat_model,
            embedding_model=settings.ollama_embed_model,
            dimensions=settings.embedding_dimensions,
            keep_alive=settings.ollama_keep_alive,
            timeout_seconds=settings.ollama_timeout_seconds,
            connect_timeout=settings.ollama_connect_timeout,
            max_batch_size=settings.ollama_max_batch_size,
        )

    if name == "gemini":
        if settings.gemini_api_key is None:
            raise ProviderNotConfigured("TUTOR_GEMINI_API_KEY is required for Gemini.")

        ollama_provider = OllamaProvider(
            base_url=settings.ollama_url,
            chat_model=settings.ollama_chat_model,
            embedding_model=settings.ollama_embed_model,
            dimensions=settings.embedding_dimensions,
            keep_alive=settings.ollama_keep_alive,
            timeout_seconds=settings.ollama_timeout_seconds,
            connect_timeout=settings.ollama_connect_timeout,
            max_batch_size=settings.ollama_max_batch_size,
        )

        return GeminiProvider(
            api_key=settings.gemini_api_key.get_secret_value(),
            chat_model=settings.gemini_chat_model,
            ollama=ollama_provider,
            timeout_seconds=settings.turn_timeout_seconds,
        )

    raise ProviderNotConfigured(f"The provider '{name}' is not implemented yet.")


def build_provider(settings: Settings) -> ModelProvider:
    """Build the model provider selected in application settings."""
    if settings.provider == "gateway":
        primary = _build_individual_provider(settings.gateway_primary, settings)
        fallback = None
        if settings.gateway_fallback != "none":
            fallback = _build_individual_provider(settings.gateway_fallback, settings)
        return LLMGatewayProvider(primary=primary, fallback=fallback)

    return _build_individual_provider(settings.provider, settings)
