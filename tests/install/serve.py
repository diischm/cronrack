"""A tiny static file server for the install tests, bound to a loopback address only.

    serve.py DIRECTORY [ADDRESS:]PORT [CERT KEY]

ADDRESS defaults to 127.0.0.1 and must be a 127.x address. With CERT and KEY it serves
https. Runs inside the throwaway test container.
"""

import functools
import http.server
import ssl
import sys


def main() -> None:
    directory = sys.argv[1]
    address, _, port = sys.argv[2].rpartition(":")
    address = address or "127.0.0.1"
    if not address.startswith("127."):
        sys.exit(f"serve.py binds to loopback addresses only, not {address}")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    server = http.server.ThreadingHTTPServer((address, int(port)), handler)
    if len(sys.argv) == 5:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(sys.argv[3], sys.argv[4])
        server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
