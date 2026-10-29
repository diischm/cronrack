"""The one way the server makes requests to the internet.

The opener refuses redirects, so a reply can never send a request on to another
address. Used for Discord webhooks (alerts.discord) and for the update checks
(clusters.updates). Both only ever request fixed, checked URLs.
"""

import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects, so a reply can't send the request on to another address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = urllib.request.build_opener(NoRedirect)
