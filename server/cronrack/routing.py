"""WebSocket URLs. Only the agent endpoint exists; browsers use plain HTTP pages."""

from django.urls import path

from agents.consumers import AgentConsumer

websocket_urlpatterns = [
    path("ws/agent/", AgentConsumer.as_asgi()),
]
