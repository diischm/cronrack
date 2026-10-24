#!/bin/sh
# Entrypoint of the CronRack server image (see server/Dockerfile). One image, several
# roles, picked by the first argument:
#
#   migrate    apply database migrations, then exit. Run it once before web and
#              scheduler start (docker compose: the one-shot "migrate" service).
#   web        (also: no arguments) the web server: pages, JSON and the agents'
#              WebSocket. Never migrates; prints the first-run setup code.
#   scheduler  the dead-man's switch loop (marks silent hosts offline, sends alerts).
#   anything else is run as-is, e.g. `python manage.py createsuperuser`.
#
# Every role ends in `exec`: the shell replaces itself with the real program, which
# then runs as PID 1 and receives the stop signal (SIGTERM) directly.
set -eu

role="${1:-web}"

case "$role" in
migrate)
    exec python manage.py migrate --noinput
    ;;

web)
    if [ "$#" -gt 1 ]; then
        echo "docker-entrypoint.sh: web takes no arguments" >&2
        exit 64
    fi

    # Clear stale "agent connected" flags and, while no account exists, print the
    # one-time setup code that /setup/ asks for (read it with: docker compose logs app).
    # This also loads the settings, so a bad setting stops here with a clear error.
    python manage.py startup

    # --proxy-headers / --forwarded-allow-ips: believe X-Forwarded-For/-Proto only from
    #   the reverse proxy's address or network (FORWARDED_ALLOW_IPS, checked by the
    #   settings above); from anyone else they're ignored.
    # --ws-*: 256 KiB max agent message, ping every 20 s, drop a socket silent for 20 s more.
    # --timeout-graceful-shutdown: on SIGTERM uvicorn stops accepting connections, closes
    #   the agents' WebSockets (close code 1012 "service restart"; agents reconnect with
    #   backoff) and gives requests in flight up to 20 s to finish, then exits. That fits
    #   inside the time the container gets before it is killed: 25 s in docker compose
    #   (stop_grace_period), 30 s by default in Kubernetes (terminationGracePeriodSeconds).
    exec uvicorn cronrack.asgi:application \
        --host 0.0.0.0 --port 8000 \
        --proxy-headers \
        --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:?FORWARDED_ALLOW_IPS must be the reverse proxy address or network}" \
        --ws-max-size 262144 \
        --ws-ping-interval 20 \
        --ws-ping-timeout 20 \
        --timeout-graceful-shutdown 20
    ;;

scheduler)
    shift
    # A pass every 30 s. The heartbeat file is touched after every successful pass, so a
    # health check (docker compose) or liveness probe can tell a working loop from a
    # stuck one. Extra arguments are appended, e.g. `scheduler --every 60`.
    exec python manage.py check_hosts --loop --every 30 \
        --heartbeat-file /tmp/check_hosts.alive "$@"
    ;;

*)
    exec "$@"
    ;;
esac
