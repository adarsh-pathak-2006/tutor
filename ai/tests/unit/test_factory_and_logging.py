import asyncio
import json
import logging

import pytest

from tests.conftest import make_settings
from tutor_ai.logging_config import JsonFormatter
from tutor_ai.providers import ProviderNotConfigured, build_provider
from tutor_ai.providers.gemini import GeminiProvider
from tutor_ai.providers.mock import MockProvider
from tutor_ai.providers.ollama import OllamaProvider
from tutor_ai.request_context import (
    new_request_id,
    reset_request_id,
    set_request_id,
)


def test_factory_builds_the_configured_provider():
    assert isinstance(
        build_provider(make_settings(TUTOR_AI_PROVIDER="mock")),
        MockProvider,
    )

    ollama = build_provider(
        make_settings(
            TUTOR_AI_PROVIDER="ollama",
            TUTOR_OLLAMA_CHAT_MODEL="llama3.1:8b",
        )
    )

    assert isinstance(ollama, OllamaProvider)
    assert ollama.chat_model == "llama3.1:8b"

    with pytest.raises(ProviderNotConfigured):
        build_provider(
            make_settings(
                TUTOR_ENV="local",
                TUTOR_AI_PROVIDER="hosted",
            )
        )


def test_factory_builds_gemini_provider():
    settings = make_settings(
        TUTOR_AI_PROVIDER="gemini",
        TUTOR_GEMINI_API_KEY="test-gemini-api-key",
        TUTOR_GEMINI_CHAT_MODEL="gemini-3.5-flash-lite",
    )

    provider = build_provider(settings)

    try:
        assert isinstance(provider, GeminiProvider)
        assert provider.name == "gemini"
        assert provider.chat_model == "gemini-3.5-flash-lite"
        assert isinstance(provider._ollama, OllamaProvider)
        assert provider._ollama.embedding_model == settings.ollama_embed_model
    finally:
        asyncio.run(provider.aclose())


def record(msg="hello", **extra):
    rec = logging.LogRecord(
        "tutor_ai.test",
        logging.INFO,
        __file__,
        1,
        msg,
        None,
        None,
    )

    for key, value in extra.items():
        setattr(rec, key, value)

    return rec


def test_json_log_line_has_request_id_and_extras():
    token = set_request_id("req-abcdef12")

    try:
        line = json.loads(JsonFormatter().format(record(chunks=4)))
    finally:
        reset_request_id(token)

    assert line["msg"] == "hello" and line["level"] == "INFO" and line["logger"] == "tutor_ai.test"
    assert line["request_id"] == "req-abcdef12"
    assert line["chunks"] == 4


def test_json_log_line_includes_tracebacks():
    try:
        raise ValueError("bad")
    except ValueError:
        import sys

        rec = logging.LogRecord(
            "x",
            logging.ERROR,
            __file__,
            1,
            "failed",
            None,
            sys.exc_info(),
        )

    line = json.loads(JsonFormatter().format(rec))

    assert "ValueError: bad" in line["exc"]
    assert line["request_id"] == "-"


@pytest.mark.parametrize(
    "bad",
    [None, "", "short", "has spaces here", "x" * 65, "a\nb-injected"],
)
def test_malformed_incoming_request_ids_are_replaced(bad):
    assert new_request_id(bad) != bad
    assert len(new_request_id(bad)) == 32


def test_well_formed_request_id_is_kept():
    assert new_request_id("django-req-1234") == "django-req-1234"
