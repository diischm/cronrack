"""
Django settings for CronRack.

Every value that differs between machines comes from environment variables
(see server/.env.example). Nothing secret is written in this file.
"""

import ipaddress
import os
import re
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.core.management.utils import get_random_secret_key

# server/ (the folder that holds manage.py)
BASE_DIR = Path(__file__).resolve().parent.parent

TRUE_WORDS = {"1", "true", "yes", "on"}
FALSE_WORDS = {"0", "false", "no", "off"}


def env_bool(name: str, default: bool = False) -> bool:
    """1/0 (or true/false, yes/no, on/off). Unset or empty: the default.

    Anything else is an error, not "off": DEBUG=ture must not quietly mean DEBUG=0.
    """
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    if value in TRUE_WORDS:
        return True
    if value in FALSE_WORDS:
        return False
    raise ImproperlyConfigured(
        f"{name} must be 1 or 0 (true/false, yes/no and on/off work too), not {value!r}."
    )


def env_list(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def env_choice(name: str, choices: tuple[str, ...], default: str) -> str:
    value = os.environ.get(name, "").strip() or default
    if value.lower() not in choices:
        raise ImproperlyConfigured(f"{name} must be one of {', '.join(choices)}, not {value!r}.")
    return value.lower()


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
if DEBUG and not ALLOWED_HOSTS:
    ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]

# Forms may only be posted from https://<domain> (checked against the Origin header),
# plus any extra origins listed in CSRF_TRUSTED_ORIGINS (comma-separated), each a full
# origin such as https://cronrack.example.com or https://cronrack.lan:8443.
_extra_origins = env_list("CSRF_TRUSTED_ORIGINS")
for _origin in _extra_origins:
    if not re.fullmatch(r"https?://[^/\s?#]+", _origin):
        raise ImproperlyConfigured(
            "CSRF_TRUSTED_ORIGINS entries must be origins such as https://cronrack.lan "
            f"(scheme and host, optional port, no path), not {_origin!r}."
        )
CSRF_TRUSTED_ORIGINS = [
    *([f"https://{CRONRACK_DOMAIN}"] if CRONRACK_DOMAIN else []),
    *(o for o in _extra_origins if o != f"https://{CRONRACK_DOMAIN}"),
]

# Which reverse proxies uvicorn believes X-Forwarded-For/-Proto from (docker-entrypoint.sh
# hands this to uvicorn --forwarded-allow-ips). Comma-separated IP addresses and/or
# networks: 172.29.80.10 (Caddy in docker compose), 10.42.0.0/16 (the k3s pod network,
# where Traefik's address changes). Checked here because uvicorn quietly ignores a typo
# such as 10.42.0.1/16 (host bits set) instead of failing. "*" (trust everyone) is
# refused: then any client could fake its address and https.
FORWARDED_ALLOW_IPS = env_list("FORWARDED_ALLOW_IPS")
for _entry in FORWARDED_ALLOW_IPS:
    try:
        if "/" in _entry:
            ipaddress.ip_network(_entry)
        else:
            ipaddress.ip_address(_entry)
    except ValueError:
        raise ImproperlyConfigured(
            "FORWARDED_ALLOW_IPS must be IP addresses or networks such as 10.42.0.0/16, "
            f"comma-separated (a network must not have host bits set), not {_entry!r}."
        ) from None

# Log level for everything CronRack and its server print (see LOGGING below).
LOG_LEVEL = env_choice(
    "LOG_LEVEL", ("debug", "info", "warning", "error", "critical"), default="info"
).upper()
# plain (default, easy to read in `docker compose logs`) or json (one JSON object per
# line, for log collectors).
LOG_FORMAT = env_choice("LOG_FORMAT", ("plain", "json"), default="plain")


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
    # Checks the Host header (ALLOWED_HOSTS): unknown names get 400 from here on.
    "django.middleware.common.CommonMiddleware",
    # Static files (/static/...), from the files collectstatic gathered at build time.
    # After CommonMiddleware on purpose (WhiteNoise's docs suggest right after
    # SecurityMiddleware): static files are only served for an allowed Host, like every
    # other page. Behind the two header middlewares above, so static responses get the
    # same security headers (HSTS, CSP, nosniff, Permissions-Policy) as pages.
    "whitenoise.middleware.WhiteNoiseMiddleware",
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
                "cronrack.context_processors.pod_identity",
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
# SQLITE_PATH: the Docker image sets /data/db.sqlite3 (its only writable volume).
# Without it (a dev machine): server/data/db.sqlite3. Must be an absolute path, so it
# can't depend on the folder the process happens to start in.
SQLITE_PATH = os.environ.get("SQLITE_PATH", "").strip() or str(BASE_DIR / "data" / "db.sqlite3")
if not Path(SQLITE_PATH).is_absolute():
    raise ImproperlyConfigured(
        f"SQLITE_PATH must be an absolute path such as /data/db.sqlite3, not {SQLITE_PATH!r}."
    )
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": SQLITE_PATH,
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


# --- Logging -----------------------------------------------------------------

# Everything goes to stdout: CronRack's own messages, Django's (including the traceback
# of every 500 error; Django's default only prints those with DEBUG=1) and uvicorn's
# (server start/stop and the access log). No log files. uvicorn sets up its own logging
# first; this replaces it when Django starts, so all lines share one format.
# The access log leaves out successful /healthz and /readyz calls (cronrack/logformat.py).
# Never logged: agent tokens, the setup code's hash, report contents. The one-time setup
# code itself is printed once on purpose (manage.py startup), not through logging.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "skip_probe_success": {"()": "cronrack.logformat.SkipProbeSuccess"},
    },
    "formatters": {
        "plain": {"format": "%(levelname)s %(name)s: %(message)s"},
        "json": {"()": "cronrack.logformat.JsonFormatter"},
    },
    "handlers": {
        "stdout": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": LOG_FORMAT,
        },
        "access": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": LOG_FORMAT,
            "filters": ["skip_probe_success"],
        },
    },
    "root": {"handlers": ["stdout"], "level": LOG_LEVEL},
    "loggers": {
        # Replaces Django's default handlers (console only with DEBUG=1, and e-mail to
        # ADMINS, which CronRack doesn't use); the messages go up to the root handler.
        "django": {"handlers": [], "level": LOG_LEVEL, "propagate": True},
        "uvicorn": {"handlers": ["stdout"], "level": LOG_LEVEL, "propagate": False},
        "uvicorn.error": {"handlers": [], "level": LOG_LEVEL, "propagate": True},
        "uvicorn.access": {"handlers": ["access"], "level": LOG_LEVEL, "propagate": False},
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
# image runs it at build time; WhiteNoise serves them from there (see MIDDLEWARE).
STATIC_ROOT = os.environ.get("STATIC_ROOT", str(BASE_DIR.parent / "staticfiles"))

# Cache-busting: collectstatic saves every file twice, as-is (components.css) and with a
# hash of its content in the name (components.3f9a2c81b4d0.css), and writes the mapping
# to STATIC_ROOT/staticfiles.json (the "manifest"). {% static %} looks names up there, so
# pages always point at the current hashed file, and a changed file gets a new URL.
# URLs inside CSS (fonts) are rewritten to the hashed names too. With DEBUG=1, {% static %}
# returns the plain names and Django serves the source files: no collectstatic needed.
# WhiteNoise's version of that storage also saves a gzipped copy of each file (.gz), and
# serves it to browsers that accept gzip.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# WhiteNoise:
# - Hashed names (components.3f9a2c81b4d0.css) always mean the same bytes: it sends
#   "Cache-Control: max-age=315360000, public, immutable", browsers never ask again.
# - MAX_AGE None: no Cache-Control from WhiteNoise for everything else (the plain names,
#   used by the 500 page). SecurityHeadersMiddleware then adds "no-cache" (the browser
#   re-checks before each use), as it does for every response without one.
# - USE_FINDERS / AUTOREFRESH off, also with DEBUG=1: WhiteNoise only ever serves the
#   collected files, read once at start. In development Django's own static view serves
#   the source files instead (cronrack/urls.py).
WHITENOISE_MAX_AGE = None
WHITENOISE_USE_FINDERS = False
WHITENOISE_AUTOREFRESH = False


# --- Security ---------------------------------------------------------------

# Cookies only travel over HTTPS. Off in DEBUG so login works on http://localhost.
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

# Behind a reverse proxy (Caddy in docker compose, Traefik in Kubernetes; set
# BEHIND_PROXY=1), TLS ends at the proxy and the app itself only sees plain http. The
# proxy says "this came in over https" with the X-Forwarded-Proto header; trust it ONLY
# in that setup. Without a proxy in front, anyone could send that header and make a
# plain-http request look secure. (Only the proxy may reach the app: in docker compose
# its port is never published, see docker-compose.yml; in Kubernetes a NetworkPolicy
# has to allow only the proxy, see docs/kubernetes.md.)
#
# USE_X_FORWARDED_HOST stays off (Django's default): Caddy and Traefik pass the original
# Host header through unchanged, so Django already sees the real name and checks it
# against ALLOWED_HOSTS. Turning it on would only add a second header to trust.
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
