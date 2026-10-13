from django.conf import settings
from django.contrib.auth import get_user_model
from django.shortcuts import redirect

SETUP_PATH = "/setup/"


class FirstRunMiddleware:
    """While no user exists, send every page to /setup/ (static files excepted).

    Once the first account exists this is one cheap EXISTS query per request,
    and /setup/ itself answers 404 from then on.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.static_prefix = "/" + settings.STATIC_URL.lstrip("/")

    def __call__(self, request):
        path = request.path
        if (
            path != SETUP_PATH
            and not path.startswith(self.static_prefix)
            and not get_user_model().objects.exists()
        ):
            return redirect(SETUP_PATH)
        return self.get_response(request)
