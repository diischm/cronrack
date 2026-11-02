"""A tiny static file server for the install tests, bound to 127.0.0.1 only.

    serve.py DIRECTORY PORT [CERT KEY]

With CERT and KEY it serves https. Runs inside the throwaway test container.
"""

import functools
import http.server
import ssl
import sys


def main() -> None:
    directory, port = sys.argv[1], int(sys.argv[2])
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    if len(sys.argv) == 5:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(sys.argv[3], sys.argv[4])
        server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
