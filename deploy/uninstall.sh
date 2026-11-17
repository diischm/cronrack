#!/usr/bin/env bash
# CronRack agent uninstaller. Removes everything deploy/install.sh put on this host and
# lists each thing it removed. It downloads nothing and runs nothing else.
#
#   sudo bash uninstall.sh            asks before deleting /etc/cronrack (it holds the token);
#                                     without a terminal, /etc/cronrack is kept
#   sudo bash uninstall.sh --purge    also deletes /etc/cronrack, without asking
#
# It is shipped inside the checksummed agent package (/opt/cronrack-agent/app/deploy/),
# served by the server at /agent/uninstall.sh, and run by `install.sh --uninstall`.
#
# What it removes (only what exists):
#   cronrack-agent.service, cronrack-docker-proxy.socket and .service (stopped, disabled,
#   unit files deleted), CronRack's drop-ins in cronrack-agent.service.d/ (egress lock, an
#   old docker-group drop-in; drop-ins you added yourself are kept), /opt/cronrack-agent, /run/cronrack, the installer's lock file,
#   the cronrack user and group, and (if you say so) /etc/cronrack.
# CronRack keeps no files in /var. Delete the host on the server too (host settings).

set -euo pipefail

# Everything is inside main(), called on the last line: bash has read the whole file before
# anything runs, so deleting /opt/cronrack-agent (where a copy of this file lives) is safe.
main() {

UNITS_DIR=/etc/systemd/system
AGENT_UNIT=cronrack-agent.service
PROXY_SOCKET=cronrack-docker-proxy.socket
PROXY_SERVICE=cronrack-docker-proxy.service
DROPIN_DIR="$UNITS_DIR/$AGENT_UNIT.d"
PREFIX=/opt/cronrack-agent
RUN_DIR=/run/cronrack
INSTALL_LOCK=/run/cronrack-agent-install.lock
CONFIG_DIR=/etc/cronrack
AGENT_USER=cronrack

say() { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
Usage: uninstall.sh [--purge]

  --purge   Also delete /etc/cronrack (agent.toml with the token, CA certificates,
            the Kubernetes token) without asking. Without --purge you are asked;
            without a terminal it is kept (owned by root).
USAGE
}

PURGE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; printf 'ERROR: unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run this with sudo (it removes a user and systemd services)"
command -v systemctl >/dev/null || die "systemd is required"

REMOVED=()
removed() { REMOVED+=("$1"); say "Removed $1"; }

# remove_unit NAME: stop and disable it, then delete its unit file.
remove_unit() {
  local name="$1"
  if [ -f "$UNITS_DIR/$name" ]; then
    systemctl disable --now --quiet "$name" 2>/dev/null || true
    rm -f "$UNITS_DIR/$name"
    removed "$UNITS_DIR/$name"
  fi
}

# The proxy socket first, so nothing starts the proxy again while it is being removed.
remove_unit "$PROXY_SOCKET"
if [ -f "$UNITS_DIR/$PROXY_SERVICE" ]; then systemctl stop "$PROXY_SERVICE" 2>/dev/null || true; fi
remove_unit "$PROXY_SERVICE"
remove_unit "$AGENT_UNIT"
# Only CronRack's own drop-ins: anything else in there is yours, so it is kept (and named).
for dropin in egress.conf docker.conf; do
  if [ -f "$DROPIN_DIR/$dropin" ]; then
    rm -f "$DROPIN_DIR/$dropin"
    removed "$DROPIN_DIR/$dropin"
  fi
done
if [ -d "$DROPIN_DIR" ]; then
  if rmdir "$DROPIN_DIR" 2>/dev/null; then
    removed "$DROPIN_DIR"
  else
    warn "kept $DROPIN_DIR: it holds files CronRack didn't write ($(find "$DROPIN_DIR" -mindepth 1 -maxdepth 1 -printf '%f ' 2>/dev/null))"
  fi
fi
if [ "${#REMOVED[@]}" -gt 0 ]; then
  systemctl daemon-reload
  systemctl reset-failed "$AGENT_UNIT" "$PROXY_SERVICE" "$PROXY_SOCKET" 2>/dev/null || true
fi

for path in "$PREFIX" "$RUN_DIR" "$INSTALL_LOCK"; do
  if [ -e "$path" ]; then
    rm -rf "$path"
    removed "$path"
  fi
done

# Asked BEFORE the user is removed: the answer decides who owns any kept files.
if [ -d "$CONFIG_DIR" ]; then
  delete_config=0
  if [ "$PURGE" -eq 1 ]; then
    delete_config=1
  elif { : </dev/tty; } 2>/dev/null; then
    answer=""
    read -r -p "Also delete $CONFIG_DIR (agent.toml with the token, CA certificates)? [y/N] " answer </dev/tty || true
    if [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]; then delete_config=1; fi
  fi
  if [ "$delete_config" -eq 1 ]; then
    rm -rf "$CONFIG_DIR"
    removed "$CONFIG_DIR"
  else
    # The cronrack group is about to disappear. A group created later could get the same
    # number and read the token, so the kept files go to root:root. A re-install gives them
    # back to cronrack.
    chown -R root:root "$CONFIG_DIR"
    say "Kept $CONFIG_DIR (now root:root). Delete it yourself with: sudo rm -rf $CONFIG_DIR"
  fi
fi

if id -u "$AGENT_USER" >/dev/null 2>&1; then
  if userdel "$AGENT_USER"; then
    removed "user $AGENT_USER"
  else
    warn "could not remove the user $AGENT_USER (is a process still running as it? see: ps -u $AGENT_USER)"
  fi
fi
# userdel removes the user's own group on Debian/Ubuntu; this covers systems where it doesn't.
if getent group "$AGENT_USER" >/dev/null; then
  if groupdel "$AGENT_USER"; then
    removed "group $AGENT_USER"
  else
    warn "could not remove the $AGENT_USER group"
  fi
fi

echo
if [ "${#REMOVED[@]}" -eq 0 ]; then
  say "Nothing to remove: no CronRack agent is installed here."
  exit 0
fi
say "The CronRack agent is uninstalled. Removed:"
for item in "${REMOVED[@]}"; do echo "    $item"; done
echo "    Delete the host on the server too (host settings)."
}

main "$@"
