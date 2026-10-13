from typing import ClassVar

from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm

from accounts.ratelimit import check_login_rate, clear_login_failures, record_login_failure

GENERIC_LOGIN_ERROR = "Wrong username or password."
RATE_LIMITED_ERROR = "Too many attempts. Try again in 15 minutes."


class LoginForm(AuthenticationForm):
    """Django's login form plus the rate limit, with one generic error message.

    The message never says whether the username or the password was wrong, and an
    inactive account gets the same message as a wrong password.
    """

    error_messages: ClassVar[dict[str, str]] = {
        "invalid_login": GENERIC_LOGIN_ERROR,
        "inactive": GENERIC_LOGIN_ERROR,
        "rate_limited": RATE_LIMITED_ERROR,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update({"class": "well", "autocomplete": "username"})
        self.fields["password"].widget.attrs.update(
            {"class": "well", "autocomplete": "current-password"}
        )

    def clean(self):
        username = self.cleaned_data.get("username") or ""
        # Checked before the password: once blocked, even the right password is refused,
        # so guessing can't continue.
        if not check_login_rate(self.request, username):
            raise forms.ValidationError(self.error_messages["rate_limited"], code="rate_limited")
        try:
            cleaned = super().clean()
        except forms.ValidationError:
            record_login_failure(self.request, username)
            raise
        clear_login_failures(self.request, username)
        return cleaned


class SetupForm(UserCreationForm):
    """First run: create the admin account (username + password twice)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update({"class": "well", "autocomplete": "username"})
        for name in ("password1", "password2"):
            self.fields[name].widget.attrs.update({"class": "well", "autocomplete": "new-password"})
