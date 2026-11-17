"""FastAPI application factory.

Run with `uvicorn tutor_ai.main:create_app --factory`; tests call create_app() with their own settings and provider.
Settings are read when the app is created, so importing this module never needs any configuration.
"""

import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__
from .api import health, v1
from .db import create_engine
from .errors import install_error_handlers
from .logging_config import configure_logging
from .middleware import RequestContextMiddleware
from .prompts import PromptError, PromptRegistry, sync_prompts
from .providers import ModelProvider, build_provider
from .safety import SafetyPolicy
from .settings import Settings, get_settings

logger = logging.getLogger("tutor_ai")

if sys.platform == "win32":  # pragma: no cover - only on native Windows
    # psycopg's async driver can't run on Windows' default (Proactor) event loop.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def create_app(settings: Settings | None = None, provider: ModelProvider | None = None) -> FastAPI:
    settings = settings or get_settings()
    if settings.environment != "test":  # tests keep pytest's log capture; everything else logs JSON to stdout
        configure_logging(settings.log_level)
    prompts = PromptRegistry.load(settings.prompts_dir)  # a malformed prompt file stops the service here
    safety = SafetyPolicy.load(settings.safety_policy)  # so does a malformed safety policy

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.engine = create_engine(
            settings.database_url.get_secret_value(),
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
        )
        app.state.provider = provider or build_provider(settings)
        app.state.prompts = prompts
        app.state.safety = safety
        try:
            await sync_prompts(app.state.engine, prompts)
        except PromptError:
            raise  # an edited prompt version must never answer students
        except Exception:  # database unreachable: start anyway (health reports it); the next start records them
            logger.warning("could not record prompt versions", exc_info=True)
        logger.info(
            "started",
            extra={"version": __version__, "environment": settings.environment, "provider": app.state.provider.name},
        )
        try:
            yield
        finally:
            await app.state.provider.aclose()
            await app.state.engine.dispose()

    app = FastAPI(
        title="TUTOR AI service",
        version=__version__,
        lifespan=lifespan,
        # Internal service: the interactive docs are only served locally.
        docs_url="/docs" if settings.environment in ("local", "test") else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.environment in ("local", "test") else None,
    )
    app.dependency_overrides[get_settings] = lambda: settings  # every dependency sees this app's settings
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(v1.router)
    return app
