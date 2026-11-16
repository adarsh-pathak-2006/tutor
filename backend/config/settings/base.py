"""Settings shared by every environment. Values that differ per environment come from env vars."""

import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

from apps.core.logging import build_logging

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # tutor/backend
REPO_DIR = BASE_DIR.parent  # tutor/
load_dotenv(REPO_DIR / ".env")  # no-op when the file doesn't exist (e.g. on Render)


def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        raise ImproperlyConfigured(f"Missing required environment variable: {name}")
    return value


def env_bool(name, default=False):
    return str(env(name, str(default))).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [item.strip() for item in env(name, default).split(",") if item.strip()]


SECRET_KEY = env("DJANGO_SECRET_KEY", required=True)
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", "")
TUTOR_DEV_TOOLS = env_bool("TUTOR_DEV_TOOLS", False)  # tester helpers; honoured only when DEBUG
TUTOR_DEMO_DATA = env_bool("TUTOR_DEMO_DATA", False)  # allows seed_demo; true locally and on the hosted demo only
TUTOR_DEMO_PASSWORD = env("TUTOR_DEMO_PASSWORD", "")  # shared by all demo people; a secret on the hosted demo
FRONTEND_URL = env("FRONTEND_URL", "http://localhost:3000")  # used in links sent to parents

# Number of reverse proxies in front of Django that append to X-Forwarded-For (Render: 1). With 0 the
# header is ignored, because a client can set it to anything. Used for rate limits and audit IPs.
TUTOR_TRUSTED_PROXIES = int(env("TUTOR_TRUSTED_PROXIES", "0"))

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # TUTOR apps
    "apps.core",
    "apps.catalogue",
    "apps.accounts",
    "apps.content",
    "apps.assessment",
    "apps.learning",
    "apps.commerce",
    "apps.operations",
    "apps.aiservice",
    "apps.tutor",
    "apps.demo",
]

MIDDLEWARE = [
    "apps.core.request_context.RequestIDMiddleware",  # first, so every later log line has the request ID
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# Database: one DATABASE_URL (local Postgres in WSL for dev, Neon in prod)
DATABASES = {
    "default": dj_database_url.parse(env("DATABASE_URL", required=True), conn_max_age=600, conn_health_checks=True)
}

# Accounts (DATA_MODEL.md M1, M2)
AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = ["apps.accounts.backends.EmailOrMobileBackend"]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Sessions and CSRF (API_CONTRACTS.md §1: session cookie + CSRF)
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

# Cache: shared by every web worker, so rate limits and login lockouts hold across processes.
# The database cache needs no extra service; move to Redis when traffic needs it (docs/engineering/deployment.md).
REDIS_URL = env("REDIS_URL", "") or env("CACHE_URL", "")
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.db.DatabaseCache",
            "LOCATION": "tutor_cache",
        }
    }

# Rate limits per client IP (per user where noted), as "count/period" (s, m, h, d). None switches one off.
# Generous on purpose: a whole class can sit behind one school IP. Accounts are protected by the lockout below.
TUTOR_THROTTLE_RATES = {
    "api": "600/m",  # every endpoint (per user when logged in)
    "login": "30/m",
    "signup": "30/h",  # student and parent sign-up, parent adding a child
    "otp": "30/h",  # send/verify one-time codes, password reset
    "consent_link": "60/h",  # parent approval-link pages
    "consent_send": "10/h",  # resend link / change parent contact (per user)
    "tutor": "20/m",  # tutor messages (per user); the daily limit is TUTOR_TUTOR_DAILY_LIMIT
}

# Login lockout per email/mobile: after this many wrong passwords the account can't log in for the window.
# Counted for unknown identifiers too, so the lockout never reveals whether an account exists.
TUTOR_LOGIN_LOCKOUT = {"max_failures": 10, "window_seconds": 15 * 60}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True  # stored in UTC, shown in IST

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Logging: console always; rotating files only when TUTOR_LOG_DIR is set (local). See apps/core/logging.py.
LOG_LEVEL = env("DJANGO_LOG_LEVEL", "INFO")
TUTOR_LOG_DIR = env("TUTOR_LOG_DIR", "")
LOGGING = build_logging(LOG_LEVEL, TUTOR_LOG_DIR)

# Keep and log the full text of outgoing SMS/email (codes, links). Local development and tests only.
TUTOR_KEEP_MESSAGES = env_bool("TUTOR_KEEP_MESSAGES", False)

# AI service (docs/architecture/ai-service.md). An empty URL means "no AI service in this environment" (the hosted
# demo today): publishing still queues index requests, and sync_ai_index reports how many are waiting.
TUTOR_AI_URL = env("TUTOR_AI_URL", "").strip().rstrip("/")
TUTOR_AI_SERVICE_TOKEN = env("TUTOR_AI_SERVICE_TOKEN", "")
TUTOR_AI_INDEX_TIMEOUT_SECONDS = float(env("TUTOR_AI_INDEX_TIMEOUT_SECONDS", "120"))  # Ollama on a CPU is slow
TUTOR_AI_INDEX_ON_PUBLISH = env_bool("TUTOR_AI_INDEX_ON_PUBLISH", True)  # send right after publishing commits

# AI tutor for students (apps/tutor, design §3, D40).
TUTOR_TUTOR_DAILY_LIMIT = int(env("TUTOR_TUTOR_DAILY_LIMIT", "50"))  # messages per student per IST day
TUTOR_AI_TURN_TIMEOUT_SECONDS = float(env("TUTOR_AI_TURN_TIMEOUT_SECONDS", "150"))  # longer than the AI service's own
TUTOR_AI_REPLY_TOKENS = int(env("TUTOR_AI_REPLY_TOKENS", "250"))
TUTOR_CHAT_RETENTION_DAYS = int(env("TUTOR_CHAT_RETENTION_DAYS", "90"))
