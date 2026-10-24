from typing import ClassVar

from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm

from accounts.ratelimit import (
    check_login_rate,
    check_setup_code_rate,
    record_setup_code_failure,
)
from accounts.setup_code import setup_code_matches

GENERIC_LOGIN_ERROR = "Wrong username or password."
RATE_LIMITED_ERROR = "Too many attempts. Try again in 15 minutes."
WRONG_SETUP_CODE_ERROR = "Wrong setup code."


class LoginForm(AuthenticationForm):
    """Django's login form with one generic error message.

    The message never says whether the username or the password was wrong, and an
    inactive account gets the same message as a wrong password.

    The rate limit itself lives in accounts.backends (it counts every failed
    authenticate(), from any form). This form only checks it first so a blocked
    user sees "Too many attempts" instead of "Wrong username or password".
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
        if not check_login_rate(self.request, username):
            raise forms.ValidationError(self.error_messages["rate_limited"], code="rate_limited")
        return super().clean()


class SetupForm(UserCreationForm):
    """First run: the setup code from the server log, then the admin account."""

    setup_code = forms.CharField(
        label="Setup code",
        max_length=64,
        help_text="Printed in the server log: docker compose logs app",
    )

    def __init__(self, *args, request=None, **kwargs):
        self.request = request
        super().__init__(*args, **kwargs)
        self.fields["setup_code"].widget.attrs.update({"class": "well", "autocomplete": "off"})
        self.fields["username"].widget.attrs.update({"class": "well", "autocomplete": "username"})
        for name in ("password1", "password2"):
            self.fields[name].widget.attrs.update({"class": "well", "autocomplete": "new-password"})

    def clean_setup_code(self):
        code = self.cleaned_data["setup_code"]
        # Checked before the code, like the login limit: once blocked, even the
        # right code is refused.
        if not check_setup_code_rate(self.request):
            raise forms.ValidationError(RATE_LIMITED_ERROR, code="rate_limited")
        if not setup_code_matches(code):
            record_setup_code_failure(self.request)
            raise forms.ValidationError(WRONG_SETUP_CODE_ERROR, code="wrong_setup_code")
        return code
