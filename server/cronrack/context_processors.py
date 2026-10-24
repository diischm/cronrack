"""Template data for every page."""

import os

from django.http import HttpRequest

from cronrack import SERVER_VERSION

# Kubernetes names are at most 253 characters; a page only needs to show a readable bit.
MAX_NAME_LENGTH = 100


def version(request: HttpRequest) -> dict:
    """The server version, for the sidebar footer and the host detail page."""
    return {"server_version": SERVER_VERSION}


def pod_identity(request: HttpRequest) -> dict:
    """Which pod and node served this page, when running in Kubernetes.

    POD_NAME, POD_NAMESPACE and NODE_NAME (all optional) are meant to be filled in by
    the Kubernetes Downward API (fieldRef metadata.name, metadata.namespace and
    spec.nodeName). Only when POD_NAME and NODE_NAME are set does the sidebar footer show
    "Served by pod ... on node ..."; otherwise (docker compose, a dev machine) nothing.
    The namespace is passed along for templates but not shown. The template escapes all
    of them like any other text.
    """
    pod, namespace, node = (
        os.environ.get(name, "").strip()[:MAX_NAME_LENGTH]
        for name in ("POD_NAME", "POD_NAMESPACE", "NODE_NAME")
    )
    if not (pod and node):
        return {"pod_identity": None}
    return {"pod_identity": {"pod": pod, "namespace": namespace, "node": node}}
