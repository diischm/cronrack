"""A tiny WebSocket test client for ASGI apps, built on asgiref's ApplicationCommunicator.

Why not the WebsocketCommunicator from Channels' testing package? Importing that package
also imports its live-server test case, which needs daphne (and with it twisted). We don't use a live
server, so this helper gives the tests the same few methods with nothing but asgiref,
which comes with Django.

How it works: an ASGI app is just an async function app(scope, receive, send).
ApplicationCommunicator calls it directly in the test's event loop, hands it two queues
(one for messages going in, one for messages coming out), and lets the test put messages
in and read messages out. No server, no socket, no port: the "network" is two queues.
This class adds the WebSocket side of the ASGI spec on top: the connection scope, and the
websocket.connect / receive / send / close / disconnect messages.

    comm = WebSocketClient(application, "/ws/agent/", headers=[(b"authorization", b"Bearer ...")])
    connected, _ = await comm.connect()
    await comm.send_json_to({"type": "hello"})
    reply = await comm.receive_json_from()
    await comm.disconnect()

receive_output(timeout) and receive_nothing(timeout) come from ApplicationCommunicator.
"""

import json
from urllib.parse import unquote

from asgiref.testing import ApplicationCommunicator


class WebSocketClient(ApplicationCommunicator):
    def __init__(self, application, path, headers=None, subprotocols=None):
        # "/ws/agent/?a=1" -> path "/ws/agent/" + query string b"a=1".
        raw_path, _, query = path.partition("?")
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "ws",
            # path is the decoded form; raw_path the bytes exactly as sent.
            "path": unquote(raw_path),
            "raw_path": raw_path.encode("ascii"),
            "query_string": query.encode("ascii"),
            "root_path": "",
            # Header names are lowercase bytes, values bytes (ASGI spec).
            "headers": [(bytes(name).lower(), bytes(value)) for name, value in (headers or [])],
            "subprotocols": list(subprotocols or []),
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
        }
        super().__init__(application, scope)

    async def connect(self, timeout=1):
        """Open the connection. Returns (True, subprotocol) if the app accepted it,
        (False, close_code) if it closed it instead."""
        await self.send_input({"type": "websocket.connect"})
        message = await self.receive_output(timeout)
        if message["type"] == "websocket.accept":
            return True, message.get("subprotocol")
        if message["type"] == "websocket.close":
            return False, message.get("code", 1000)
        raise AssertionError(f"unexpected reply to websocket.connect: {message!r}")

    async def send_to(self, text_data=None, bytes_data=None):
        """Send one frame to the app: text or bytes, exactly one of them."""
        if (text_data is None) == (bytes_data is None):
            raise TypeError("pass exactly one of text_data or bytes_data")
        if text_data is not None:
            await self.send_input({"type": "websocket.receive", "text": text_data})
        else:
            await self.send_input({"type": "websocket.receive", "bytes": bytes_data})

    async def send_json_to(self, data):
        await self.send_to(text_data=json.dumps(data))

    async def receive_from(self, timeout=1):
        """The next frame the app sends: str for a text frame, bytes for a binary one.
        Anything else (e.g. websocket.close) fails the test."""
        message = await self.receive_output(timeout)
        if message["type"] != "websocket.send":
            raise AssertionError(f"expected websocket.send, got {message!r}")
        if message.get("text") is not None:
            return message["text"]
        return message["bytes"]

    async def receive_json_from(self, timeout=1):
        data = await self.receive_from(timeout)
        if not isinstance(data, str):
            raise TypeError(f"expected a JSON text frame, got bytes: {data!r}")
        return json.loads(data)

    async def disconnect(self, code=1000, timeout=1):
        """The client goes away: tell the app, then wait for it to finish."""
        await self.send_input({"type": "websocket.disconnect", "code": code})
        await self.wait(timeout)
