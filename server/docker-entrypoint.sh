#!/bin/sh
# Entrypoint of the CronRack server image (see server/Dockerfile).
#
#   no arguments  -> the "app" service: migrate the database, hand the static files to
#                    Caddy, then run uvicorn
#   arguments     -> run exactly that instead, e.g. the "scheduler" service runs
#                    python manage.py check_hosts --loop --every 30
set -eu

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

# Settings refuse to load without SECRET_KEY, so a missing key stops here with a clear error.
python manage.py migrate --noinput

# Caddy serves /static/* from the shared "static" volume. Refresh it from this image
# on every start, so an upgraded image never leaves Caddy serving old CSS/JS.
if [ -d /srv/static ]; then
    find /srv/static -mindepth 1 -delete
    cp -R /app/staticfiles/. /srv/static/
fi

# exec: uvicorn replaces this shell, so it gets Docker's stop signal directly.
# --proxy-headers / --forwarded-allow-ips: believe X-Forwarded-For/-Proto only from
#   Caddy's address (set in docker-compose.yml); from anyone else they're ignored.
# --ws-*: 256 KiB max agent message, ping every 20 s, drop a socket silent for 20 s more.
exec uvicorn cronrack.asgi:application \
    --host 0.0.0.0 --port 8000 \
    --proxy-headers \
    --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:?FORWARDED_ALLOW_IPS must be the reverse proxy address}" \
    --ws-max-size 262144 \
    --ws-ping-interval 20 \
    --ws-ping-timeout 20
