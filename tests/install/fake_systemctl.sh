#!/usr/bin/env bash
# A fake systemctl for the install tests (the test containers have no systemd).
# It records every call in /var/log/fake-systemctl.log. "restart" imports the installed
# agent with the venv's python: if that fails, the service counts as failed, like an
# agent that crashes on start. Create /etc/fake-systemctl-fail to make every start fail,
# and /etc/fake-systemctl-reload-fail to make daemon-reload fail (an error mid-install).
# Like the real one, "disable" only stops the service with --now.
set -u

LOG=/var/log/fake-systemctl.log
STATE=/run/fake-systemctl.state
ENABLED=/run/fake-systemctl.enabled
printf '%s\n' "$*" >>"$LOG"

if [ "${1:-}" = daemon-reload ] && [ -e /etc/fake-systemctl-reload-fail ]; then
  echo "fake systemctl: daemon-reload failed" >&2
  exit 1
fi

now=0
args=()
for arg in "$@"; do
  case "$arg" in
    --now) now=1 ;;
    --quiet|-q) ;;
    *) args+=("$arg") ;;
  esac
done

case "${args[0]:-}" in
  restart|start)
    if [ ! -e /etc/fake-systemctl-fail ] \
      && (cd /opt/cronrack-agent/app \
        && /opt/cronrack-agent/venv/bin/python -c 'import agent.__main__') >/dev/null 2>&1; then
      echo active >"$STATE"
    else
      echo failed >"$STATE"
    fi
    ;;
  stop)
    echo inactive >"$STATE"
    ;;
  enable)
    echo enabled >"$ENABLED"
    ;;
  disable)
    echo disabled >"$ENABLED"
    if [ "$now" -eq 1 ]; then echo inactive >"$STATE"; fi
    ;;
  is-active)
    [ "$(cat "$STATE" 2>/dev/null)" = active ]
    exit
    ;;
  is-enabled)
    [ "$(cat "$ENABLED" 2>/dev/null)" = enabled ]
    exit
    ;;
esac
exit 0
