"""
Django settings for CronRack.

Every value that differs between machines comes from environment variables
(see server/.env.example). Nothing secret is written in this file.
"""

import os
import re
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

# The one name the server is reached by, e.g. "cronrack.home.lan" (docker compose sets
# it from .env). It goes into ALLOWED_HOSTS and CSRF_TRUSTED_ORIGINS, and the
# "Add host" page builds the agent's install command from it.
# Same characters install.sh accepts: letters, digits, dots and dashes (no port).
CRONRACK_DOMAIN = os.environ.get("CRONRACK_DOMAIN", "").strip().lower()
if CRONRACK_DOMAIN and not re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?", CRONRACK_DOMAIN):
    raise ImproperlyConfigured(
        "CRONRACK_DOMAIN must be a bare host name such as cronrack.home.lan "
        "(no https://, no port, no path)."
    )

# Comma-separated extra names, e.g. "cronrack.home.lan,localhost". With DEBUG=1 and
# nothing set, only this machine: localhost, 127.0.0.1 and [::1].
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS")
if CRONRACK_DOMAIN:
    ALLOWED_HOSTS = [CRONRACK_DOMAIN, *(h for h in ALLOWED_HOSTS if h != CRONRACK_DOMAIN)]
    # Forms may only be posted from https://<domain> (checked against the Origin header).
    CSRF_TRUSTED_ORIGINS = [f"https://{CRONRACK_DOMAIN}"]
if DEBUG and not ALLOWED_HOSTS:
    ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]


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
                "hosts.context_processors.shell",
                "cronrack.context_processors.version",
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
# Where `collectstatic` gathers every static file (ours and the admin's). The Docker
# image runs it at build time; Caddy then serves those files, not Django.
STATIC_ROOT = os.environ.get("STATIC_ROOT", str(BASE_DIR.parent / "staticfiles"))


# --- Security ---------------------------------------------------------------

# Cookies only travel over HTTPS. Off in DEBUG so login works on http://localhost.
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

# Behind Caddy (docker compose sets BEHIND_PROXY=1), TLS ends at Caddy and the app
# itself only sees plain http. Caddy says "this came in over https" with the
# X-Forwarded-Proto header; trust it ONLY in that setup. Without a proxy in front,
# anyone could send that header and make a plain-http request look secure.
# (Only Caddy can reach the app: its port is never published, see docker-compose.yml.)
BEHIND_PROXY = env_bool("BEHIND_PROXY", default=False)
if BEHIND_PROXY:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Redirect http -> https and tell browsers to use https only (HSTS).
SECURE_SSL_REDIRECT = not DEBUG
# Except /healthz: Docker's health check calls it over plain http inside the container.
# It returns "ok" and nothing else.
SECURE_REDIRECT_EXEMPT = [r"^healthz$"]
SECURE_HSTS_SECONDS = 0 if DEBUG else 31_536_000  # one year
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
# Not preloaded: preloading is a public, hard-to-undo registration that only makes
# sense for a public domain, not a self-hosted box on a VPN.
SECURE_HSTS_PRELOAD = False

# Never allow CronRack pages inside a frame (clickjacking).
X_FRAME_OPTIONS = "DENY"
