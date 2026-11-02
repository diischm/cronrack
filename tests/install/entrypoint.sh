#!/usr/bin/env bash
# Start of an install-test container: serve the agent package that /srv/www points at,
# over http on 127.0.0.1:8000 and https on 127.0.0.1:443 (the throwaway CA in /srv/tls).
# The tests switch packages by pointing /srv/www at another directory in /pkgs.
set -eu

ln -sfn /pkgs/current /srv/www
python3 /usr/local/lib/installtest/serve.py /srv/www 8000 &
python3 /usr/local/lib/installtest/serve.py /srv/www 443 /srv/tls/server.crt /srv/tls/server.key &
exec sleep infinity
