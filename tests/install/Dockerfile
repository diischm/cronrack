# Test image for tests/install: deploy/install.sh runs in it as root, in a throwaway
# container started with --network none. Built by tests/install/conftest.py from a
# temporary build context (this file, the scripts next to it, agent/requirements.txt).
FROM debian:12-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv curl ca-certificates openssl \
 && rm -rf /var/lib/apt/lists/*

# A stand-in for PyPI, so install.sh's pip runs without a network: the agent's one dependency
# as a wheel (downloaded now, at build time) and a PEP 503 index page for it at
# /simple/websockets/. entrypoint.sh serves it as https://pypi.org on 127.0.0.2:443.
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /tmp/dl \
 && /tmp/dl/bin/pip download --no-cache-dir --disable-pip-version-check --only-binary=:all: \
      -d /srv/pypi/files -r /tmp/requirements.txt \
 && mkdir -p /srv/pypi/simple/websockets \
 && cd /srv/pypi/files \
 && for f in *; do \
      printf '<a href="../../files/%s#sha256=%s">%s</a>\n' \
        "$f" "$(sha256sum "$f" | cut -d' ' -f1)" "$f"; \
    done >/srv/pypi/simple/websockets/index.html \
 && rm -rf /tmp/dl /tmp/requirements.txt

# Its own throwaway CA, trusted system-wide (Debian's pip uses the system store), that signs
# only "pypi.org". The CA for "localhost" below stays untrusted, as before.
RUN mkdir -p /srv/pypi-tls && cd /srv/pypi-tls \
 && openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=installtest fake PyPI CA" \
      -keyout ca.key -out ca.crt 2>/dev/null \
 && openssl req -newkey rsa:2048 -nodes -subj "/CN=pypi.org" \
      -keyout server.key -out server.csr 2>/dev/null \
 && printf 'subjectAltName=DNS:pypi.org\n' >ext.cnf \
 && openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 30 \
      -extfile ext.cnf -out server.crt 2>/dev/null \
 && cp ca.crt /usr/local/share/ca-certificates/installtest-fake-pypi.crt \
 && update-ca-certificates >/dev/null \
 && rm -f ca.key server.csr ext.cnf

# A pip setup that breaks any pip that reads it, like piwheels on a Raspberry Pi but louder:
# no-index in /etc/pip.conf and in the environment. install.sh's pip must ignore both
# (PIP_CONFIG_FILE=/dev/null, --isolated), so every install test checks that it does.
RUN printf '[global]\nno-index = true\nfind-links = /nonexistent\n' >/etc/pip.conf
ENV PIP_NO_INDEX=1 PIP_FIND_LINKS=/nonexistent

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
