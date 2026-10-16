"""Rotate token through the UI: the settings page hands out a one-time key the POST must carry."""

import re

from django.test import Client

ROTATE_KEY_RE = re.compile(r'<input type="hidden" name="rotate_key" value="([A-Za-z0-9_-]{16,})">')


def rotate_key(client: Client, host_id: int) -> str:
    """Open host settings (as the user would) and return the Rotate form's one-time key."""
    response = client.get(f"/hosts/{host_id}/settings/")
    assert response.status_code == 200
    match = ROTATE_KEY_RE.search(response.content.decode())
    assert match, "host settings should render the Rotate form's one-time key"
    return match.group(1)


def post_rotate(client: Client, host_id: int, key: str | None = None):
    """Press Rotate token and confirm it: a fresh key from the settings page unless one is given."""
    if key is None:
        key = rotate_key(client, host_id)
    return client.post(f"/hosts/{host_id}/rotate/", {"rotate_key": key, "confirm": "yes"})
