#!/usr/bin/env bash
# Smoke test for the docker compose stack. From the repository root:
#
#   scripts/smoke_test.sh
#
# It builds the image from the current code and starts the whole stack (app,
# scheduler, caddy) under a throwaway project name, then checks:
#   1. /healthz answers "ok" through Caddy over HTTPS, verified against Caddy's own CA
#   2. plain http redirects to https
#   3. a TLS handshake WITHOUT a server name (SNI) works, as when CRONRACK_DOMAIN is an IP
#   4. the agent WebSocket (/ws/agent/) refuses a handshake without a token
#   5. only Caddy publishes ports; app and scheduler publish none
#   6. all three containers become healthy
# and always tears everything down again (containers, network, volumes, its image).
#
# It never reads or changes your .env and never touches a running CronRack stack:
# it uses its own settings file, volumes, network (172.29.81.0/24) and ports, bound
# to 127.0.0.1 only. Change the ports with SMOKE_HTTP_PORT / SMOKE_HTTPS_PORT.

set -euo pipefail

HTTP_PORT="${SMOKE_HTTP_PORT:-18080}"
HTTPS_PORT="${SMOKE_HTTPS_PORT:-18443}"
PROJECT="cronrack-smoke-$$"
IMAGE="cronrack-server:smoke-$$"
SUBNET=172.29.81.0/24
IP_RANGE=172.29.81.128/25
CADDY_IP=172.29.81.10
BASE="https://localhost:$HTTPS_PORT"
TIMEOUT=240  # seconds to wait for health checks (the scheduler waits for the app first)

say() { printf '==> %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

cd "$(dirname "$0")/.."
command -v docker >/dev/null || fail "docker is not installed"
command -v curl >/dev/null || fail "curl is not installed"
command -v openssl >/dev/null || fail "openssl is not installed"
docker compose version >/dev/null 2>&1 || fail "the docker compose plugin is not installed"

TMP="$(mktemp -d)"
ENV_FILE="$TMP/smoke.env"
OVERRIDE="$TMP/override.yml"
ROOT_CA="$TMP/root.crt"

compose() {
  docker compose -p "$PROJECT" -f docker-compose.yml -f "$OVERRIDE" --env-file "$ENV_FILE" "$@"
}

cleanup() {
  local status=$?
  if [ "$status" -ne 0 ] && [ -f "$OVERRIDE" ]; then
    say "Logs (last 30 lines per service)"
    compose logs --tail 30 || true
  fi
  say "Tearing down $PROJECT"
  compose down --volumes --remove-orphans >/dev/null 2>&1 || true
  docker image rm "$IMAGE" >/dev/null 2>&1 || true
  rm -rf "$TMP"
  exit "$status"
}
trap cleanup EXIT

# A random throwaway key: hex only, so docker compose reads it as-is.
SECRET_KEY="$(od -An -tx1 -N32 /dev/urandom | tr -d ' \n')"
cat >"$ENV_FILE" <<EOF
CRONRACK_DOMAIN=localhost
SECRET_KEY=$SECRET_KEY
BEHIND_PROXY=1
DEBUG=0
EOF

# Same stack, different image tag, ports, network and settings file. `!override`
# replaces the list from docker-compose.yml instead of adding to it.
cat >"$OVERRIDE" <<EOF
services:
  app:
    image: $IMAGE
    env_file: !override
      - $ENV_FILE
    environment:
      FORWARDED_ALLOW_IPS: $CADDY_IP
  scheduler:
    image: $IMAGE
    env_file: !override
      - $ENV_FILE
  caddy:
    ports: !override
      - "127.0.0.1:$HTTP_PORT:80"
      - "127.0.0.1:$HTTPS_PORT:443"
    networks:
      cronrack:
        ipv4_address: $CADDY_IP
networks:
  cronrack:
    ipam:
      config: !override
        - subnet: $SUBNET
          ip_range: $IP_RANGE
EOF

say "Building and starting $PROJECT"
compose config --quiet
compose up -d --build --quiet-pull

# --- 1. /healthz through Caddy ---------------------------------------------------------

say "Waiting for /healthz through Caddy"
deadline=$((SECONDS + TIMEOUT))
until compose exec -T caddy cat /data/caddy/pki/authorities/local/root.crt >"$ROOT_CA" 2>/dev/null \
  && [ "$(curl -fsS --max-time 5 --cacert "$ROOT_CA" "$BASE/healthz" 2>/dev/null)" = "ok" ]; do
  [ "$SECONDS" -lt "$deadline" ] || fail "/healthz did not answer \"ok\" within ${TIMEOUT}s"
  sleep 3
done
say "OK: $BASE/healthz answers \"ok\" (certificate verified against Caddy's root CA)"

# --- 2. http -> https -------------------------------------------------------------------

redirect="$(curl -sS --max-time 5 -o /dev/null -w '%{http_code} %{redirect_url}' "http://localhost:$HTTP_PORT/")"
case "$redirect" in
  30[178]\ https://localhost*) say "OK: plain http redirects to https ($redirect)" ;;
  *) fail "plain http did not redirect to https: $redirect" ;;
esac

# --- 3. TLS without SNI ------------------------------------------------------------------

# Browsers, curl and the agent send no server name when they connect to an IP address.
# Without `default_sni` in the Caddyfile, Caddy then aborts with "tlsv1 alert internal
# error". Connect by IP with -noservername; the certificate must still verify for the
# domain. (Exit status AND "Verification: OK": on a failed handshake s_client still
# prints "Verify return code: 0", because nothing was verified.)
if ! no_sni="$(openssl s_client -connect "127.0.0.1:$HTTPS_PORT" -noservername \
  -CAfile "$ROOT_CA" -verify_hostname localhost -verify_return_error </dev/null 2>&1)" \
  || ! grep -q '^Verification: OK' <<<"$no_sni"; then
  printf '%s\n' "$no_sni" | grep -Ei 'alert|error|verif' >&2 || true
  fail "TLS handshake without SNI failed (default_sni missing from the Caddyfile?)"
fi
say "OK: a TLS handshake without SNI gets the certificate for localhost, verified"

# --- 4. the agent WebSocket needs a token -------------------------------------------------

# A complete WebSocket handshake, but no Authorization header. 101 would mean "accepted".
# Sec-WebSocket-Key is a fresh nonce per handshake: 16 random bytes in base64.
WS_KEY="$(head -c 16 /dev/urandom | base64)"
ws_code="$(curl --http1.1 -sS --max-time 5 -o /dev/null -w '%{http_code}' --cacert "$ROOT_CA" \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
  -H "Sec-WebSocket-Key: ${WS_KEY}" "$BASE/ws/agent/" || true)"
[ "$ws_code" = "403" ] || fail "/ws/agent/ without a token answered $ws_code, expected 403"
say "OK: /ws/agent/ refuses a handshake without a token (403)"

# --- 5. only Caddy publishes ports ------------------------------------------------------

for service in app scheduler; do
  id="$(compose ps -q "$service")"
  [ -n "$id" ] || fail "$service is not running"
  [ -z "$(docker port "$id")" ] || fail "$service publishes ports: $(docker port "$id" | tr '\n' ' ')"
done
caddy_ports="$(docker port "$(compose ps -q caddy)" | sort -u | tr '\n' ' ')"
for port in "$HTTP_PORT" "$HTTPS_PORT"; do
  [[ "$caddy_ports" == *"127.0.0.1:$port"* ]] || fail "caddy does not publish port $port ($caddy_ports)"
done
say "OK: only caddy publishes ports ($caddy_ports)"

# --- 6. every container healthy -------------------------------------------------------------

say "Waiting for all three containers to be healthy"
until [ "$(compose ps --format '{{.Health}}' | grep -c '^healthy$')" -eq 3 ]; do
  [ "$SECONDS" -lt "$deadline" ] || { compose ps; fail "not all containers became healthy within ${TIMEOUT}s"; }
  sleep 5
done
compose ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'
say "OK: app, scheduler and caddy are healthy"

echo
say "Smoke test passed"
