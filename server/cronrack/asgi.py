"""
ASGI entry point. uvicorn runs this: `uvicorn cronrack.asgi:application`.

ProtocolTypeRouter sends each connection to the right handler by protocol.
Only "http" exists for now; the agent "websocket" route is added in Phase 5.
"""

import os

from channels.routing import ProtocolTypeRouter
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cronrack.settings")

# Set up Django before importing anything that touches models.
django_asgi_app = get_asgi_application()

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
    }
)
