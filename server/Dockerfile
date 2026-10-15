# CronRack server image. Built by docker compose from the repository root:
#
#   docker compose build          (context: the repo root, see docker-compose.yml)
#
# Two stages: "build" installs the Python packages into a virtual environment;
# the final stage copies only that venv and our code. pip, build caches and
# anything else from the build stage never reach the final image.

# The exact Python image, pinned by digest (the digest covers amd64 and arm64).
# Upgrade on purpose: change the tag AND the digest together
# (docker buildx imagetools inspect python:<tag> prints the digest).
ARG PYTHON_IMAGE=python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1


# --- stage 1: build the virtual environment --------------------------------------------
FROM ${PYTHON_IMAGE} AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
COPY server/requirements.txt /tmp/requirements.txt
# --only-binary: install ready-made wheels only, never compile anything, so no compiler
# is needed (and none is installed). pip is removed afterwards: the running server
# never installs packages, so it has no business carrying a package installer.
RUN /opt/venv/bin/pip install --only-binary=:all: -r /tmp/requirements.txt \
    && /opt/venv/bin/pip uninstall --yes pip


# --- stage 2: the image that actually runs ----------------------------------------------
FROM ${PYTHON_IMAGE}

# PATH: use the venv's python/uvicorn. PYTHONDONTWRITEBYTECODE: the filesystem is
# read-only at runtime (bytecode is compiled below instead). PYTHONUNBUFFERED: logs
# show up in `docker compose logs` immediately.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# The base image's own pip goes too (the venv above has none): nothing in this image
# installs packages at runtime.
# A fixed, unprivileged user. Fixed IDs mean files in the data volume keep the same
# owner across rebuilds. No home directory and no login shell.
RUN python -m pip uninstall --yes --quiet pip \
    && groupadd --system --gid 10001 cronrack \
    && useradd --system --uid 10001 --gid cronrack --no-create-home \
       --home-dir /nonexistent --shell /usr/sbin/nologin cronrack

COPY --from=build /opt/venv /opt/venv

# Our code, owned by root: the server process can read it but never change it.
# agent/ and deploy/ are here because the server builds the agent download and
# serves install.sh from them (server/agents/package.py).
WORKDIR /app
COPY server/ server/
COPY agent/ agent/
COPY deploy/ deploy/

# - collectstatic: gather all CSS/JS (ours and the admin's) into /app/staticfiles, each
#   file also under a content-hashed name, plus the manifest staticfiles.json that
#   {% static %} reads at runtime (settings.STORAGES). Built into the image, so every
#   image has a manifest that matches its own files.
#   Settings need a SECRET_KEY to load; this throwaway one exists only for this one
#   command and is not stored in the image.
# - compileall: precompile .py to .pyc now, since nothing can be written at runtime.
# - data/ (SQLite) and /srv/static (the files handed to Caddy) belong to the app
#   user; Docker copies that ownership into the named volumes mounted there.
RUN SECRET_KEY=collectstatic-only STATIC_ROOT=/app/staticfiles \
       python server/manage.py collectstatic --noinput --verbosity 0 \
    && python -m compileall -q server agent \
    && install -d -o cronrack -g cronrack -m 0750 server/data \
    && install -d -o cronrack -g cronrack -m 0755 /srv/static

WORKDIR /app/server
USER 10001:10001

# Documentation only: the app listens on 8000 inside the compose network.
# docker-compose.yml does NOT publish it; only Caddy can reach it.
EXPOSE 8000

# Docker marks the container unhealthy if /healthz stops answering. /healthz is
# answered before Django's Host check, so this works whatever CRONRACK_DOMAIN is.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request as r; r.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]

# Run through sh so the script works whatever its file mode is.
ENTRYPOINT ["/bin/sh", "/app/server/docker-entrypoint.sh"]
