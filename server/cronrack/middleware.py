"""Security headers (and a default Cache-Control) for every HTTP response.

Static files included: WhiteNoise sits after this middleware (settings.MIDDLEWARE), so
its responses pass through here too. Caddy used to add Cache-Control; the app does it
itself now, so it is the same behind any reverse proxy (or none).

Referrer-Policy, Cross-Origin-Opener-Policy and X-Content-Type-Options come from
Django's SecurityMiddleware (its defaults: same-origin, same-origin, nosniff), and
X-Frame-Options from XFrameOptionsMiddleware. tests/test_view_security.py checks
that they are all present.
"""

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

# Browser features CronRack never uses: no page (or anything injected into one) may
# ask for the camera, microphone, location or payment.
PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=()"

DEFAULT_CACHE_CONTROL = "no-cache"


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response["Permissions-Policy"] = PERMISSIONS_POLICY
        # Pages, JSON and un-hashed static files: the browser must re-check before each
        # use (a page names the current hashed CSS/JS, so it must never be stale). Only
        # when the response has no Cache-Control of its own: some views send a stricter
        # one, and WhiteNoise marks hashed files as cacheable forever.
        if not response.has_header("Cache-Control"):
            response["Cache-Control"] = DEFAULT_CACHE_CONTROL
        return response
