"""The login rate limit, enforced for every authenticate() call.

Set in settings.AUTHENTICATION_BACKENDS, so it covers /login/, /admin/login/ and any
login form added later: they all end up in django.contrib.auth.authenticate(), which
asks this backend. A form can't forget the limit by not calling it.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.core.exceptions import PermissionDenied

from accounts.ratelimit import check_login_rate, clear_login_failures, record_login_failure


class RateLimitedModelBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            username = kwargs.get(get_user_model().USERNAME_FIELD)
        username = username or ""
        # Checked before the password: once blocked, even the right password is
        # refused, so guessing can't continue. PermissionDenied makes authenticate()
        # stop and return None without asking any other backend.
        if not check_login_rate(request, username):
            raise PermissionDenied("Too many failed login attempts")
        user = super().authenticate(request, username=username, password=password, **kwargs)
        # None also covers an inactive account (ModelBackend refuses it): that counts
        # as a failure too.
        if user is None:
            record_login_failure(request, username)
        else:
            clear_login_failures(request, username)
        return user
