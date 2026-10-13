#!/usr/bin/env bash
# CronRack agent installer. Run on the host you want to monitor:
#
#   curl -fsSL https://SERVER/agent/install.sh | sudo bash -s -- --server https://SERVER
#
# (http://localhost, http://127.0.0.1 and http://[::1] are allowed for local testing only.)
#
# What it does:
#   1. creates the unprivileged "cronrack" system user
#   2. downloads the agent from YOUR server and checks its SHA-256 before unpacking
#   3. installs it in /opt/cronrack-agent/{app,venv} (root-owned: the agent can't modify itself)
#   4. asks for the token (hidden input; never a command-line argument, never in shell history)
#      and writes /etc/cronrack/agent.toml as root:cronrack, mode 0640
#   5. asks whether the agent may read Docker (docker group = root-equivalent; default: no)
#   6. installs and starts the cronrack-agent systemd service
#
# Re-running it upgrades the agent in place and keeps the existing config.
# This file contains no secrets; it's served publicly by the CronRack server.

set -euo pipefail

PREFIX=/opt/cronrack-agent
APP_DIR="$PREFIX/app"
VENV_DIR="$PREFIX/venv"
CONFIG_DIR=/etc/cronrack
CONFIG_FILE="$CONFIG_DIR/agent.toml"
UNIT_NAME=cronrack-agent.service
UNIT_FILE="/etc/systemd/system/$UNIT_NAME"
DOCKER_DROPIN_DIR="/etc/systemd/system/$UNIT_NAME.d"
DOCKER_DROPIN="$DOCKER_DROPIN_DIR/docker.conf"
AGENT_USER=cronrack
MIN_PYTHON="3.11"

say() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
Usage: install.sh --server https://SERVER

  --server URL   Base URL of your CronRack server, e.g. https://cronrack.lan
                 (http:// only for localhost / 127.0.0.1 / [::1], for local testing)
USAGE
}

# --- arguments --------------------------------------------------------------------

SERVER=""
while [ $# -gt 0 ]; do
  case "$1" in
    --server) [ $# -ge 2 ] || die "--server needs a URL"; SERVER="$2"; shift 2 ;;
    --server=*) SERVER="${1#--server=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done
[ -n "$SERVER" ] || { usage >&2; die "--server is required"; }
SERVER="${SERVER%/}"

# Same rule as the agent (agent/config.py): https, or plain http only to this machine.
# Only simple characters, so the URL is safe to put in the TOML file and in curl.
if [[ "$SERVER" =~ ^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$ ]]; then
  CURL_PROTO="=https"
elif [[ "$SERVER" =~ ^http://(localhost|127\.0\.0\.1|\[::1\])(:[0-9]{1,5})?/?$ ]]; then
  CURL_PROTO="=http"
  say "WARNING: plain http:// to $SERVER: only for local testing (e.g. an SSH tunnel)."
else
  die "--server must be https://HOST (plain http:// is only allowed for localhost, 127.0.0.1 and [::1])"
fi

# --- checks -----------------------------------------------------------------------

[ "$(id -u)" -eq 0 ] || die "run this with sudo (it creates a user and a systemd service)"
command -v systemctl >/dev/null || die "systemd is required"
command -v curl >/dev/null || die "curl is required (sudo apt install curl)"
command -v sha256sum >/dev/null || die "sha256sum is required (coreutils)"
command -v tar >/dev/null || die "tar is required"

command -v python3 >/dev/null || die "python3 is required (version $MIN_PYTHON or newer): sudo apt install python3 python3-venv"
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  die "Python $MIN_PYTHON or newer is required, found $(python3 -V 2>&1).
       The agent uses tomllib (new in 3.11), and its websockets library needs 3.11 too.
       Debian 12 / Raspberry Pi OS 12 / Ubuntu 22.04+ ship a new enough python3."
fi
if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
  die "Python's venv module is missing: sudo apt install python3-venv"
fi

# Questions are read from the terminal, because stdin is the script itself (curl | bash).
[ -r /dev/tty ] || die "no terminal to ask questions on; run this from an interactive shell"

ask_yes_no() {  # ask_yes_no "Question? [y/N] " -> 0 for yes
  local answer=""
  read -r -p "$1" answer </dev/tty || true
  [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]
}

# --- 1. user ----------------------------------------------------------------------

if id -u "$AGENT_USER" >/dev/null 2>&1; then
  say "User $AGENT_USER exists"
else
  say "Creating system user $AGENT_USER (no login, no home)"
  useradd --system --user-group --no-create-home --home-dir /nonexistent \
    --shell /usr/sbin/nologin "$AGENT_USER"
fi

# --- 2. download and verify --------------------------------------------------------

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fetch() {  # fetch URL FILE
  curl -fsSL --proto "$CURL_PROTO" --tlsv1.2 --retry 3 -o "$2" "$1" \
    || die "download failed: $1"
}

say "Downloading the agent from $SERVER"
fetch "$SERVER/agent/download/cronrack-agent.tar.gz" "$TMP/cronrack-agent.tar.gz"
fetch "$SERVER/agent/download/cronrack-agent.tar.gz.sha256" "$TMP/cronrack-agent.tar.gz.sha256"

EXPECTED="$(awk 'NR==1 {print $1}' "$TMP/cronrack-agent.tar.gz.sha256")"
[[ "$EXPECTED" =~ ^[0-9a-f]{64}$ ]] || die "the .sha256 file from the server is malformed"
ACTUAL="$(sha256sum "$TMP/cronrack-agent.tar.gz" | awk '{print $1}')"
[ "$EXPECTED" = "$ACTUAL" ] || die "checksum mismatch: the download is corrupt or was tampered with; nothing was installed"
say "Checksum OK ($ACTUAL)"

# --- 3. install code and venv ------------------------------------------------------

say "Installing to $PREFIX"
install -d -m 0755 -o root -g root "$PREFIX"
STAGING="$PREFIX/app.new"
rm -rf "$STAGING"
install -d -m 0755 -o root -g root "$STAGING"
tar -xzf "$TMP/cronrack-agent.tar.gz" -C "$STAGING" --no-same-owner --no-same-permissions
[ -f "$STAGING/agent/__main__.py" ] && [ -f "$STAGING/agent/requirements.txt" ] \
  && [ -f "$STAGING/deploy/$UNIT_NAME" ] || die "the downloaded package looks incomplete"
chown -R root:root "$STAGING"
chmod -R u=rwX,go=rX "$STAGING"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  say "Creating the Python virtual environment"
  python3 -m venv "$VENV_DIR"
fi
say "Installing the agent's one dependency (websockets)"
"$VENV_DIR/bin/pip" install --quiet --disable-pip-version-check --no-cache-dir \
  --upgrade -r "$STAGING/agent/requirements.txt"

# Swap in the new code only now that everything above worked.
rm -rf "$PREFIX/app.old"
[ -d "$APP_DIR" ] && mv "$APP_DIR" "$PREFIX/app.old"
mv "$STAGING" "$APP_DIR"
rm -rf "$PREFIX/app.old"
# The service can't write to /opt (ProtectSystem=strict), so compile the .pyc files now.
"$VENV_DIR/bin/python" -m compileall -q "$APP_DIR/agent" >/dev/null

# --- 4. config and token -------------------------------------------------------------

install -d -m 0750 -o root -g "$AGENT_USER" "$CONFIG_DIR"
if [ -f "$CONFIG_FILE" ]; then
  say "Keeping the existing $CONFIG_FILE (to change the token or server, edit it or delete it and re-run)"
  chown root:"$AGENT_USER" "$CONFIG_FILE"
  chmod 0640 "$CONFIG_FILE"
else
  echo
  echo "Paste the agent token shown on the server's \"Add host\" page."
  echo "(Typing is hidden. The token is not shown again on the server.)"
  TOKEN=""
  read -r -s -p "Token: " TOKEN </dev/tty || true
  echo
  [[ "$TOKEN" =~ ^[A-Za-z0-9_-]{20,128}$ ]] || die "that doesn't look like a CronRack token; nothing was written"
  # Create the file with its final owner and mode BEFORE the token goes in,
  # so there is never a moment where others could read it.
  install -m 0640 -o root -g "$AGENT_USER" /dev/null "$CONFIG_FILE"
  # printf is a bash builtin: the token never appears in the process list.
  printf 'server_url = "%s"\ntoken = "%s"\n' "$SERVER" "$TOKEN" >"$CONFIG_FILE"
  unset TOKEN
  say "Wrote $CONFIG_FILE (root:$AGENT_USER, 0640)"
fi

# --- 5. Docker access (opt-in) ---------------------------------------------------------

echo
echo "The agent can check your Docker containers, but only if it's in the docker group."
echo "Members of the docker group can control Docker, which is equivalent to root on this host."
if ask_yes_no "Give the agent Docker access? (docker group = root-equivalent) [y/N] "; then
  getent group docker >/dev/null || die "there is no docker group on this host (is Docker installed?)"
  install -d -m 0755 "$DOCKER_DROPIN_DIR"
  printf '[Service]\nSupplementaryGroups=docker\n' >"$DOCKER_DROPIN"
  chmod 0644 "$DOCKER_DROPIN"
  say "Docker access enabled ($DOCKER_DROPIN)"
else
  rm -f "$DOCKER_DROPIN"
  say "No Docker access (container checks will report \"unavailable\")"
fi

# --- 6. systemd service -------------------------------------------------------------------

install -m 0644 -o root -g root "$APP_DIR/deploy/$UNIT_NAME" "$UNIT_FILE"
systemctl daemon-reload
systemctl enable --quiet "$UNIT_NAME"
systemctl restart "$UNIT_NAME"

echo
say "Done. The agent is running and connecting to $SERVER."
echo "    Status:  systemctl status cronrack-agent"
echo "    Logs:    journalctl -u cronrack-agent -f"
echo "    Upgrade: re-run the same install command (the config is kept)"
