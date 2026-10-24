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
    # First: answers GET /healthz before the Host check, redirects or sessions
    # (see cronrack/health.py).
    "cronrack.health.HealthCheckMiddleware",
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

# The app and the scheduler (two containers) write to the same SQLite file on the
# shared data volume. WAL mode lets readers and one writer work at the same time; its
# helper files (db.sqlite3-wal, db.sqlite3-shm) sit next to db.sqlite3 in data/.
# WAL needs a local filesystem shared by one machine, which a Docker volume is
# (never put this file on NFS/SMB).
# - transaction_mode IMMEDIATE: a transaction takes the write lock when it starts, not
#   halfway through, so two writers queue up instead of failing with "database is locked".
# - timeout: wait up to 20 s for that lock.
# - synchronous=NORMAL: the usual setting with WAL; a power cut can lose the last
#   moments of writes, but never corrupts the database.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("SQLITE_PATH", str(BASE_DIR / "data" / "db.sqlite3")),
        "OPTIONS": {
            "transaction_mode": "IMMEDIATE",
            "timeout": 20,
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
        },
    }
}


# --- Auth (Django's built-in accounts only) ---------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# Django's normal username/password check plus the login rate limit, so every login
# (/login/, /admin/login/, anything added later) is rate-limited (accounts/backends.py).
AUTHENTICATION_BACKENDS = ["accounts.backends.RateLimitedModelBackend"]

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"

# Django's admin at /admin/. Off on a server unless asked for: CronRack's own pages
# cover everything, and a second login page is one more thing to attack.
# Default: on with DEBUG=1, off otherwise. CRONRACK_ENABLE_ADMIN=1 turns it on.
CRONRACK_ENABLE_ADMIN = env_bool("CRONRACK_ENABLE_ADMIN", default=DEBUG)

# In-memory cache, used for the login rate limit. Each process has its own copy,
# which is fine with the single uvicorn process CronRack runs.
# MAX_ENTRIES: Django's default is 300, after which it throws old entries away. Lockout
# entries must not be pushed out that easily (accounts/ratelimit.py also limits how many
# entries one IP can create).
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "OPTIONS": {"MAX_ENTRIES": 10_000},
    },
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

# Cache-busting: collectstatic saves every file twice, as-is (components.css) and with a
# hash of its content in the name (components.3f9a2c81b4d0.css), and writes the mapping
# to STATIC_ROOT/staticfiles.json (the "manifest"). {% static %} looks names up there, so
# pages always point at the current hashed file, and a changed file gets a new URL.
# URLs inside CSS (fonts) are rewritten to the hashed names too. With DEBUG=1, {% static %}
# returns the plain names and Django serves the source files: no collectstatic needed.
# Caddy may cache the hashed files "forever" (see caddy/Caddyfile).
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"},
}


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
SECURE_HSTS_SECONDS = 0 if DEBUG else 31_536_000  # one year
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
# Not preloaded: preloading is a public, hard-to-undo registration that only makes
# sense for a public domain, not a self-hosted box on a VPN.
SECURE_HSTS_PRELOAD = False

# Never allow CronRack pages inside a frame (clickjacking).
X_FRAME_OPTIONS = "DENY"
