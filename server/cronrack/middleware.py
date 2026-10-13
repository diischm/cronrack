"""Security headers for every HTTP response."""

# Only files from this server may load: no inline <script>, no inline style="",
# no third-party CDNs, never inside a frame, and forms only post back to us.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self'; "
    "script-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'"
)


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        return response
