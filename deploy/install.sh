#!/usr/bin/env bash
# CronRack agent installer. Run on the host you want to monitor:
#
#   curl -fsSL https://SERVER/agent/install.sh | sudo bash -s -- --server https://SERVER
#
# If the server uses Caddy's own CA (`tls internal`), copy its root certificate to the
# host first and pass it to both curl and the installer:
#
#   curl -fsSL --cacert cronrack-ca.crt https://SERVER/agent/install.sh \
#     | sudo bash -s -- --server https://SERVER --ca-file cronrack-ca.crt
#
# (http://localhost, http://127.0.0.1 and http://[::1] are allowed for local testing only.)
#
# What it does:
#   1. creates the unprivileged "cronrack" system user
#   2. downloads the agent from YOUR server and checks its SHA-256 before unpacking
#   3. installs it in /opt/cronrack-agent/{app,venv} (root-owned: the agent can't modify itself)
#   4. asks for the token (hidden input; never a command-line argument, never in shell history)
#      and writes /etc/cronrack/agent.toml as root:cronrack, mode 0640
#   5. if Docker is installed, asks whether the agent may read it
#      (docker group = root-equivalent; default: no)
#   6. installs and starts the cronrack-agent systemd service
#
# Kubernetes (optional, off unless you ask; see docs/kubernetes-monitoring.md):
#   --k8s                  report this host's Kubernetes role (k3s server / agent / kubelet).
#                          Asks nothing and needs no token: run it on every cluster node.
#   --k8s-cluster-view     also let this agent read the cluster through the Kubernetes API.
#                          Implies --k8s. Run it on ONE node only. Asks for a read-only
#                          service account token (hidden input) and copies the cluster's CA
#                          certificate (default: /var/lib/rancher/k3s/server/tls/server-ca.crt).
#   --k8s-ca PATH          use this CA certificate instead (only with --k8s-cluster-view)
#   --k8s-api URL          API server address (default https://127.0.0.1:6443)
#
# Re-running it upgrades the agent in place and keeps the existing config and token:
#   --server URL     always updates server_url in the existing config
#   --new-token      asks for a new token and replaces only the token
#   --ca-file PATH   copies the CA certificate to /etc/cronrack/ca.crt and sets ca_file
#   --k8s...         only changes the [kubernetes] settings you name; the rest is kept
#
# Remove the agent again (asks before deleting /etc/cronrack, which holds the token):
#
#   sudo bash install.sh --uninstall
#
# This file contains no secrets; it's served publicly by the CronRack server.

set -euo pipefail

PREFIX=/opt/cronrack-agent
APP_DIR="$PREFIX/app"
VENV_DIR="$PREFIX/venv"
CONFIG_DIR=/etc/cronrack
CONFIG_FILE="$CONFIG_DIR/agent.toml"
CA_DEST="$CONFIG_DIR/ca.crt"
K8S_TOKEN_FILE="$CONFIG_DIR/k8s-token"
K8S_CA_DEST="$CONFIG_DIR/k8s-ca.crt"
K8S_CA_DEFAULT=/var/lib/rancher/k3s/server/tls/server-ca.crt
UNIT_NAME=cronrack-agent.service
UNIT_FILE="/etc/systemd/system/$UNIT_NAME"
DOCKER_DROPIN_DIR="/etc/systemd/system/$UNIT_NAME.d"
DOCKER_DROPIN="$DOCKER_DROPIN_DIR/docker.conf"
AGENT_USER=cronrack
MIN_PYTHON="3.11"

say() { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Questions are read from the terminal, because stdin is the script itself (curl | bash).
ask_yes_no() {  # ask_yes_no "Question? [y/N] " -> 0 for yes
  local answer=""
  read -r -p "$1" answer </dev/tty || true
  [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]
}

usage() {
  cat <<'USAGE'
Usage: install.sh --server https://SERVER [--ca-file PATH] [--new-token]
                  [--k8s | --k8s-cluster-view [--k8s-ca PATH] [--k8s-api URL]]
       install.sh --uninstall

  --server URL     Base URL of your CronRack server, e.g. https://cronrack.lan
                   (http:// only for localhost / 127.0.0.1 / [::1], for local testing)
  --ca-file PATH   PEM root certificate the server's certificate is signed by, for a
                   private CA such as Caddy's `tls internal`. Used by curl here and
                   copied to /etc/cronrack/ca.crt for the agent.
  --new-token      On a re-run: ask for a new token (default: keep the existing one)
  --k8s            Report this host's Kubernetes role (k3s server/agent). No token needed.
  --k8s-cluster-view
                   Also read the cluster via the Kubernetes API (implies --k8s). Use it on
                   ONE node. Asks for a read-only service account token (hidden input).
  --k8s-ca PATH    Cluster CA certificate to copy (default: the k3s server CA). Public
                   certificate only: files with a private key are refused.
  --k8s-api URL    Kubernetes API address, https only (default https://127.0.0.1:6443)
  --uninstall      Stop and remove the service, /opt/cronrack-agent and the cronrack
                   user; asks before deleting /etc/cronrack
USAGE
}

# --- arguments --------------------------------------------------------------------

SERVER=""
CA_FILE=""
NEW_TOKEN=0
UNINSTALL=0
K8S=0
K8S_VIEW=0
K8S_CA=""
K8S_API=""
while [ $# -gt 0 ]; do
  case "$1" in
    --server) [ $# -ge 2 ] || die "--server needs a URL"; SERVER="$2"; shift 2 ;;
    --server=*) SERVER="${1#--server=}"; shift ;;
    --ca-file) [ $# -ge 2 ] || die "--ca-file needs a path"; CA_FILE="$2"; shift 2 ;;
    --ca-file=*) CA_FILE="${1#--ca-file=}"; shift ;;
    --new-token) NEW_TOKEN=1; shift ;;
    --k8s) K8S=1; shift ;;
    --k8s-cluster-view) K8S=1; K8S_VIEW=1; shift ;;
    --k8s-ca) [ $# -ge 2 ] || die "--k8s-ca needs a path"; K8S_CA="$2"; shift 2 ;;
    --k8s-ca=*) K8S_CA="${1#--k8s-ca=}"; shift ;;
    --k8s-api) [ $# -ge 2 ] || die "--k8s-api needs a URL"; K8S_API="$2"; shift 2 ;;
    --k8s-api=*) K8S_API="${1#--k8s-api=}"; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done
# --- uninstall ------------------------------------------------------------------------

uninstall() {
  [ "$(id -u)" -eq 0 ] || die "run this with sudo (it removes a user and a systemd service)"
  command -v systemctl >/dev/null || die "systemd is required"

  say "Stopping and removing the $UNIT_NAME service"
  systemctl disable --now --quiet "$UNIT_NAME" 2>/dev/null || true
  rm -f "$UNIT_FILE"
  rm -rf "$DOCKER_DROPIN_DIR"
  systemctl daemon-reload
  systemctl reset-failed "$UNIT_NAME" 2>/dev/null || true

  say "Removing $PREFIX"
  rm -rf "$PREFIX"

  # Asked BEFORE the user is removed: the answer decides who owns any kept files.
  if [ -d "$CONFIG_DIR" ]; then
    if [ -r /dev/tty ] && ask_yes_no "Also delete $CONFIG_DIR (agent.toml with the token, ca.crt)? [y/N] "; then
      rm -rf "$CONFIG_DIR"
      say "Deleted $CONFIG_DIR"
    else
      # The cronrack group is about to disappear. A group created later could get the
      # same number and read the token, so the kept files go to root:root. A re-install
      # gives them back to cronrack.
      chown -R root:root "$CONFIG_DIR"
      say "Kept $CONFIG_DIR (now root:root). Delete it yourself with: sudo rm -rf $CONFIG_DIR"
    fi
  fi

  if id -u "$AGENT_USER" >/dev/null 2>&1; then
    say "Removing the $AGENT_USER user"
    userdel "$AGENT_USER"
  fi
  # userdel removes the user's own group on Debian/Ubuntu; this covers systems where it doesn't.
  if getent group "$AGENT_USER" >/dev/null; then
    groupdel "$AGENT_USER" || warn "could not remove the $AGENT_USER group"
  fi

  echo
  say "The CronRack agent is uninstalled. Delete the host on the server too (host settings)."
}

if [ "$UNINSTALL" -eq 1 ]; then
  uninstall
  exit 0
fi

# --- install: check the arguments ------------------------------------------------------

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

# Same rule as the agent (agent/config.py): the Kubernetes API is https only, no path.
if [ -n "$K8S_CA" ] || [ -n "$K8S_API" ]; then
  [ "$K8S_VIEW" -eq 1 ] || die "--k8s-ca and --k8s-api only make sense with --k8s-cluster-view"
fi
if [ -n "$K8S_API" ]; then
  K8S_API="${K8S_API%/}"
  [[ "$K8S_API" =~ ^https://([A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])(:[0-9]{1,5})?$ ]] \
    || die "--k8s-api must be https://HOST[:PORT] (no path; plain http:// is not allowed)"
fi

# curl trusts exactly this CA (not the system's) when --ca-file is given.
CURL_CA=()
if [ -n "$CA_FILE" ]; then
  if [ ! -f "$CA_FILE" ] || [ ! -r "$CA_FILE" ]; then die "--ca-file: cannot read $CA_FILE"; fi
  grep -q -- '-----BEGIN CERTIFICATE-----' "$CA_FILE" \
    || die "--ca-file: $CA_FILE is not a PEM certificate (expected -----BEGIN CERTIFICATE-----)"
  CURL_CA=(--cacert "$CA_FILE")
elif [ -f "$CA_DEST" ]; then
  # A re-run without --ca-file: keep trusting the CA installed last time, the one
  # the agent's config already points at.
  CURL_CA=(--cacert "$CA_DEST")
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

[ -r /dev/tty ] || die "no terminal to ask questions on; run this from an interactive shell"

# --- Kubernetes: check everything and ask for the token BEFORE changing anything ------------

# check_k8s_ca FILE: a readable PEM certificate, never a private key.
check_k8s_ca() {
  local file="$1"
  if [ ! -f "$file" ] || [ ! -r "$file" ]; then
    die "Kubernetes CA: cannot read $file (pass --k8s-ca PATH)"
  fi
  [[ "$file" != *.key ]] || die "Kubernetes CA: $file is a .key file; pass the public .crt"
  if grep -q 'PRIVATE KEY' "$file"; then
    die "Kubernetes CA: $file contains a private key; pass the public certificate only"
  fi
  grep -q -- '-----BEGIN CERTIFICATE-----' "$file" \
    || die "Kubernetes CA: $file is not a PEM certificate"
}

# ask_k8s_token: reads the service account token into K8S_TOKEN, hidden. It is only ever
# written to a file with printf (a bash builtin), never printed or passed as an argument.
ask_k8s_token() {
  echo
  echo "Paste the Kubernetes service account token for the cluster view."
  echo "(Typing is hidden. It must be a read-only token; see docs/kubernetes-monitoring.md.)"
  K8S_TOKEN=""
  read -r -s -p "Kubernetes token: " K8S_TOKEN </dev/tty || true
  echo
  if [ "${#K8S_TOKEN}" -gt 12000 ] \
    || ! [[ "$K8S_TOKEN" =~ ^[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}$ ]]; then
    die "that doesn't look like a Kubernetes service account token; nothing was changed"
  fi
}

K8S_TOKEN=""
K8S_CA_SRC=""
if [ "$K8S_VIEW" -eq 1 ]; then
  if [ -n "$K8S_CA" ]; then
    K8S_CA_SRC="$K8S_CA"
  elif [ -f "$K8S_CA_DEFAULT" ]; then
    K8S_CA_SRC="$K8S_CA_DEFAULT"
  elif [ -f "$K8S_CA_DEST" ]; then
    K8S_CA_SRC="$K8S_CA_DEST"  # a re-run on a host without the k3s server CA: keep the copy
  else
    die "Kubernetes CA: $K8S_CA_DEFAULT not found; pass the cluster CA certificate with --k8s-ca PATH"
  fi
  check_k8s_ca "$K8S_CA_SRC"
  if [ -f "$K8S_TOKEN_FILE" ]; then
    if ask_yes_no "A Kubernetes token is already installed. Replace it? [y/N] "; then
      ask_k8s_token
    else
      say "Keeping the existing Kubernetes token"
    fi
  else
    ask_k8s_token
  fi
fi

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
  curl -fsSL --proto "$CURL_PROTO" --tlsv1.2 --retry 3 "${CURL_CA[@]}" -o "$2" "$1" \
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
if [ ! -f "$STAGING/agent/__main__.py" ] || [ ! -f "$STAGING/agent/requirements.txt" ] \
  || [ ! -f "$STAGING/deploy/$UNIT_NAME" ]; then
  die "the downloaded package looks incomplete"
fi
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

ask_token() {  # reads the token into TOKEN, hidden; dies before anything is written
  echo
  echo "Paste the agent token shown on the server's \"Add host\" page."
  echo "(Typing is hidden. The token is not shown again on the server.)"
  TOKEN=""
  read -r -s -p "Token: " TOKEN </dev/tty || true
  echo
  [[ "$TOKEN" =~ ^[A-Za-z0-9_-]{20,128}$ ]] || die "that doesn't look like a CronRack token; nothing was changed"
}

# set_config_key KEY VALUE: in $CONFIG_FILE, replace the top-level `KEY = ...` line (or
# add one) and keep every other line as it is. Only bash builtins touch the value, so
# a token never shows up in the process list. The new file is created root:cronrack
# 0640 before anything is written to it, then moved over the old one in one step.
set_config_key() {
  local key="$1" value="$2" tmp line written=0 in_table=0
  tmp="$(mktemp "$CONFIG_DIR/.agent.toml.XXXXXX")"
  if ! { chown root:"$AGENT_USER" "$tmp" && chmod 0640 "$tmp"; }; then
    rm -f "$tmp"
    die "cannot prepare $tmp"
  fi
  {
    while IFS= read -r line || [ -n "$line" ]; do
      # Everything after the first [table] header belongs to that table, so a
      # missing top-level key is added before it, not at the end of the file.
      if [ "$in_table" -eq 0 ] && [[ "$line" =~ ^[[:space:]]*\[ ]]; then
        in_table=1
        if [ "$written" -eq 0 ]; then printf '%s = "%s"\n' "$key" "$value"; written=1; fi
      fi
      if [ "$in_table" -eq 0 ] && [[ "$line" =~ ^[[:space:]]*${key}[[:space:]]*= ]]; then
        if [ "$written" -eq 0 ]; then printf '%s = "%s"\n' "$key" "$value"; written=1; fi
        continue
      fi
      printf '%s\n' "$line"
    done <"$CONFIG_FILE"
    if [ "$written" -eq 0 ]; then printf '%s = "%s"\n' "$key" "$value"; fi
  } >"$tmp" || { rm -f "$tmp"; die "cannot write $tmp"; }
  mv -f "$tmp" "$CONFIG_FILE"
}

# set_kubernetes_table KEY=VALUE...: in $CONFIG_FILE, set these keys inside the
# [kubernetes] table (created if missing) and keep every other line, including the
# table's other keys, as it is. VALUE is already TOML (true, or "quoted"). Bash builtins
# only, new file root:cronrack 0640 before it is written, then moved over the old one.
set_kubernetes_table() {
  local tmp line pair key in_table=0 seen=0 skip
  tmp="$(mktemp "$CONFIG_DIR/.agent.toml.XXXXXX")"
  if ! { chown root:"$AGENT_USER" "$tmp" && chmod 0640 "$tmp"; }; then
    rm -f "$tmp"
    die "cannot prepare $tmp"
  fi
  {
    while IFS= read -r line || [ -n "$line" ]; do
      if [[ "$line" =~ ^[[:space:]]*\[ ]]; then
        # A table header ends the [kubernetes] table: add the keys it was missing.
        if [ "$in_table" -eq 1 ]; then printf '%s\n' "$@"; fi
        in_table=0
        if [[ "$line" =~ ^[[:space:]]*\[kubernetes\][[:space:]]*(#.*)?$ ]]; then
          in_table=1
          seen=1
        fi
      elif [ "$in_table" -eq 1 ]; then
        skip=0
        for pair in "$@"; do
          key="${pair%%=*}"
          if [[ "$line" =~ ^[[:space:]]*${key}[[:space:]]*= ]]; then skip=1; fi
        done
        if [ "$skip" -eq 1 ]; then continue; fi
      fi
      printf '%s\n' "$line"
    done <"$CONFIG_FILE"
    if [ "$in_table" -eq 1 ]; then
      printf '%s\n' "$@"
    elif [ "$seen" -eq 0 ]; then
      printf '\n[kubernetes]\n'
      printf '%s\n' "$@"
    fi
  } >"$tmp" || { rm -f "$tmp"; die "cannot write $tmp"; }
  mv -f "$tmp" "$CONFIG_FILE"
}

install -d -m 0750 -o root -g "$AGENT_USER" "$CONFIG_DIR"
if [ -f "$CONFIG_FILE" ]; then
  say "Keeping the existing $CONFIG_FILE"
  chown root:"$AGENT_USER" "$CONFIG_FILE"
  chmod 0640 "$CONFIG_FILE"
  # A kept CA certificate (e.g. after --uninstall kept /etc/cronrack) must be readable again.
  if [ -f "$CA_DEST" ]; then
    chown root:"$AGENT_USER" "$CA_DEST"
    chmod 0640 "$CA_DEST"
  fi
  # Kept Kubernetes files (e.g. after --uninstall kept /etc/cronrack) must be readable again.
  for kept in "$K8S_TOKEN_FILE" "$K8S_CA_DEST"; do
    if [ -f "$kept" ]; then
      chown root:"$AGENT_USER" "$kept"
      chmod 0640 "$kept"
    fi
  done
  set_config_key server_url "$SERVER"
  say "server_url = \"$SERVER\""
  # grep only reads the file; the token itself never becomes an argument.
  if [ "$NEW_TOKEN" -eq 1 ] || ! grep -Eq '^[[:space:]]*token[[:space:]]*=' "$CONFIG_FILE"; then
    ask_token
    set_config_key token "$TOKEN"
    unset TOKEN
    say "Token replaced"
  else
    say "Token kept (pass --new-token to replace it)"
  fi
else
  ask_token
  # Create the file with its final owner and mode BEFORE the token goes in,
  # so there is never a moment where others could read it.
  install -m 0640 -o root -g "$AGENT_USER" /dev/null "$CONFIG_FILE"
  # printf is a bash builtin: the token never appears in the process list.
  printf 'server_url = "%s"\ntoken = "%s"\n' "$SERVER" "$TOKEN" >"$CONFIG_FILE"
  unset TOKEN
  say "Wrote $CONFIG_FILE (root:$AGENT_USER, 0640)"
fi

if [ -n "$CA_FILE" ]; then
  # Readable by the agent, not by everyone (it's public, but there's no reason to share).
  install -m 0640 -o root -g "$AGENT_USER" "$CA_FILE" "$CA_DEST"
  set_config_key ca_file "$CA_DEST"
  say "Installed the CA certificate as $CA_DEST (root:$AGENT_USER, 0640)"
fi

if [ "$K8S" -eq 1 ]; then
  if [ "$K8S_VIEW" -eq 0 ]; then
    set_kubernetes_table 'enabled = true'
    say "Kubernetes: this host reports its node role ([kubernetes] enabled = true)"
  else
    # The token file is created root:cronrack 0640 BEFORE the token goes in.
    if [ -n "$K8S_TOKEN" ]; then
      install -m 0640 -o root -g "$AGENT_USER" /dev/null "$K8S_TOKEN_FILE"
      printf '%s\n' "$K8S_TOKEN" >"$K8S_TOKEN_FILE"
      K8S_TOKEN=""
      say "Wrote $K8S_TOKEN_FILE (root:$AGENT_USER, 0640)"
    fi
    if [ "$K8S_CA_SRC" != "$K8S_CA_DEST" ]; then
      install -m 0640 -o root -g "$AGENT_USER" "$K8S_CA_SRC" "$K8S_CA_DEST"
    fi
    say "Installed the Kubernetes CA certificate as $K8S_CA_DEST"
    set_kubernetes_table 'enabled = true' 'cluster_view = true' \
      "token_file = \"$K8S_TOKEN_FILE\"" "ca_file = \"$K8S_CA_DEST\""
    if [ -n "$K8S_API" ]; then set_kubernetes_table "api_server = \"$K8S_API\""; fi
    say "Kubernetes: cluster view on ([kubernetes] cluster_view = true)"
  fi
fi

# The config is complete. From here on, nothing optional may stop the install:
# the service below must always be installed and started.

# --- 5. Docker access (opt-in, only if Docker is installed) --------------------------------

echo
if getent group docker >/dev/null; then
  echo "The agent can check your Docker containers, but only if it's in the docker group."
  echo "Members of the docker group can control Docker, which is equivalent to root on this host."
  if ask_yes_no "Give the agent Docker access? (docker group = root-equivalent) [y/N] "; then
    if install -d -m 0755 "$DOCKER_DROPIN_DIR" \
      && printf '[Service]\nSupplementaryGroups=docker\n' >"$DOCKER_DROPIN" \
      && chmod 0644 "$DOCKER_DROPIN"; then
      say "Docker access enabled ($DOCKER_DROPIN)"
    else
      rm -f "$DOCKER_DROPIN" || true
      warn "could not enable Docker access; container checks stay off. Re-run the installer to try again."
    fi
  else
    rm -f "$DOCKER_DROPIN" || true
    say "No Docker access (container checks will report \"unavailable\")"
  fi
else
  # A leftover drop-in naming a group that no longer exists would stop the service from starting.
  rm -f "$DOCKER_DROPIN" || true
  say "Docker not found: container checks will be off. Re-run this installer after installing Docker to enable them."
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
echo "    Upgrade: re-run the same install command (the config and token are kept)"
if [ "$K8S_VIEW" -eq 1 ]; then
  echo "    Check the Kubernetes access:"
  echo "      cd $APP_DIR && sudo -u $AGENT_USER ../venv/bin/python -m agent --check-k8s --config $CONFIG_FILE"
fi
