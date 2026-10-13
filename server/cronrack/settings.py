"""
Django settings for CronRack.

Every value that differs between machines comes from environment variables
(see server/.env.example). Nothing secret is written in this file.
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.core.management.utils import get_random_secret_key

# server/ (the folder that holds manage.py)
BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


# --- Core -------------------------------------------------------------------

DEBUG = env_bool("DEBUG", default=False)

SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured(
            "SECRET_KEY environment variable is required (set DEBUG=1 for local development)."
        )
    # Development only: a throwaway key per process. Logins reset on every restart.
    SECRET_KEY = get_random_secret_key()

# Comma-separated, e.g. "cronrack.home.lan,localhost". With DEBUG=1 and nothing set,
# Django still allows localhost / 127.0.0.1 / [::1].
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS")


# --- Applications -----------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "channels",
    "accounts",
    "hosts",
    "agents",
    "activity",
    "alerts",
    "prefs",
    "rules",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Content-Security-Policy on every response.
    "cronrack.middleware.SecurityHeadersMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # While no account exists, every page redirects to /setup/.
    "accounts.middleware.FirstRunMiddleware",
]

ROOT_URLCONF = "cronrack.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # Project-wide templates (base.html, components/, hosts/) live in server/templates/.
        "DIRS": [BASE_DIR / "templates"],
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

# ASGI only: uvicorn serves both HTTP and (from Phase 5) the agent WebSocket.
ASGI_APPLICATION = "cronrack.asgi.application"

# One uvicorn process is enough for a homelab, so the in-memory layer is fine.
# Redis would only be needed with several server processes.
CHANNEL_LAYERS = {
    "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
}


# --- Database ---------------------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("SQLITE_PATH", str(BASE_DIR / "data" / "db.sqlite3")),
    }
}


# --- Auth (Django's built-in accounts only) ---------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"

# In-memory cache, used for the login rate limit. Each process has its own copy,
# which is fine with the single uvicorn process CronRack runs.
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
}


# --- Time and language ------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True


# --- Static files -----------------------------------------------------------

STATIC_URL = "static/"
# Project-wide CSS/JS/fonts live in server/static/cronrack/.
STATICFILES_DIRS = [BASE_DIR / "static"]


# --- Security ---------------------------------------------------------------

# Cookies only travel over HTTPS. Off in DEBUG so login works on http://localhost.
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

# Redirect http -> https and tell browsers to use https only (HSTS).
SECURE_SSL_REDIRECT = not DEBUG
SECURE_HSTS_SECONDS = 0 if DEBUG else 31_536_000  # one year
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
# Not preloaded: preloading is a public, hard-to-undo registration that only makes
# sense for a public domain, not a self-hosted box on a VPN.
SECURE_HSTS_PRELOAD = False

# Never allow CronRack pages inside a frame (clickjacking).
X_FRAME_OPTIONS = "DENY"
