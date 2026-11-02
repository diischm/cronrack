# Test image for tests/install: deploy/install.sh runs in it as root, in a throwaway
# container started with --network none. Built by tests/install/conftest.py from a
# temporary build context (this file, the scripts next to it, agent/requirements.txt).
FROM debian:12-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv curl ca-certificates openssl \
 && rm -rf /var/lib/apt/lists/*

# The agent's one dependency as a wheel, so install.sh's pip runs without a network.
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /tmp/dl \
 && /tmp/dl/bin/pip download --no-cache-dir --disable-pip-version-check --only-binary=:all: \
      -d /wheels -r /tmp/requirements.txt \
 && rm -rf /tmp/dl /tmp/requirements.txt
ENV PIP_NO_INDEX=1 PIP_FIND_LINKS=/wheels

# A throwaway CA and a certificate for "localhost", made at build time (never committed).
RUN mkdir -p /srv/tls && cd /srv/tls \
 && openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=installtest CA" \
      -keyout ca.key -out ca.crt 2>/dev/null \
 && openssl req -newkey rsa:2048 -nodes -subj "/CN=localhost" \
      -keyout server.key -out server.csr 2>/dev/null \
 && printf 'subjectAltName=DNS:localhost\n' >ext.cnf \
 && openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 30 \
      -extfile ext.cnf -out server.crt 2>/dev/null \
 && rm -f ca.key server.csr ext.cnf

COPY fake_systemctl.sh /usr/local/bin/systemctl
COPY entrypoint.sh /usr/local/lib/installtest/entrypoint.sh
COPY serve.py /usr/local/lib/installtest/serve.py
RUN chmod 0755 /usr/local/bin/systemctl /usr/local/lib/installtest/entrypoint.sh

CMD ["/usr/local/lib/installtest/entrypoint.sh"]
