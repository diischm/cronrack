from django.contrib.auth import get_user_model, login
from django.contrib.auth.views import LoginView
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from accounts.forms import WRONG_SETUP_CODE_ERROR, LoginForm, SetupForm
from accounts.setup_code import claim_setup_code, ensure_setup_code
from prefs.models import Prefs


def create_first_admin(form: SetupForm):
    """Create the admin account from a valid SetupForm. None if someone else was faster.

    The account and the claimed setup code are saved in one transaction. On SQLite
    the transaction starts with BEGIN IMMEDIATE (settings: transaction_mode), so two
    of these never run at the same time: the second one waits, then sees the account
    and the missing code, and creates nothing. claim_setup_code() deleting exactly
    one row is a second guard that also holds on other databases.
    """
    User = get_user_model()
    with transaction.atomic():
        if User.objects.exists() or not claim_setup_code(form.cleaned_data["setup_code"]):
            return None
        user = form.save(commit=False)
        user.is_staff = True
        user.is_superuser = True
        user.save()
        Prefs.objects.create(owner=user)
    return user


@never_cache
@require_http_methods(["GET", "POST"])
def first_run(request: HttpRequest) -> HttpResponse:
    """Create the admin account. Only works while no account exists; 404 afterwards.

    Without the 404, anyone who can reach the server could create a second admin later.
    It also needs the setup code from the server log (accounts.setup_code).
    """
    User = get_user_model()
    if User.objects.exists():
        raise Http404
    ensure_setup_code()
    form = SetupForm(request.POST or None, request=request)
    if request.method == "POST" and form.is_valid():
        user = create_first_admin(form)
        if user is None:
            if User.objects.exists():
                raise Http404
            form.add_error("setup_code", WRONG_SETUP_CODE_ERROR)
        else:
            login(request, user)
            return redirect("overview")
    return render(request, "accounts/setup.html", {"form": form})


class CronRackLoginView(LoginView):
    template_name = "accounts/login.html"
    form_class = LoginForm
    redirect_authenticated_user = True
