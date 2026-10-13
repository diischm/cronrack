"""
ASGI entry point. uvicorn runs this: `uvicorn cronrack.asgi:application`.

ProtocolTypeRouter sends each connection to the right handler by protocol:
- "http":      every normal page, the admin and (in DEBUG) static files
- "websocket": only the agent endpoint, /ws/agent/
"""

import os

from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cronrack.settings")

# Set up Django before importing anything that touches models.
django_asgi_app = get_asgi_application()

# Imported after setup on purpose: the consumer imports models.
from cronrack.routing import websocket_urlpatterns

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        # Deliberately NOT wrapped in AuthMiddlewareStack: the agent socket
        # authenticates with its token only, never with a browser's login cookie
        # (see AgentConsumer for why: cross-site WebSocket hijacking).
        "websocket": URLRouter(websocket_urlpatterns),
    }
)
