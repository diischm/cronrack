from django.contrib.auth import get_user_model, login
from django.contrib.auth.views import LoginView
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from accounts.forms import LoginForm, SetupForm
from prefs.models import Prefs


@never_cache
@require_http_methods(["GET", "POST"])
def first_run(request: HttpRequest) -> HttpResponse:
    """Create the admin account. Only works while no account exists; 404 afterwards.

    Without the 404, anyone who can reach the server could create a second admin later.
    """
    User = get_user_model()
    if User.objects.exists():
        raise Http404
    form = SetupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            # Checked again inside the transaction: two browsers submitting at once
            # must not both create an admin.
            if User.objects.exists():
                raise Http404
            user = form.save(commit=False)
            user.is_staff = True
            user.is_superuser = True
            user.save()
            Prefs.objects.create(owner=user)
        login(request, user)
        return redirect("overview")
    return render(request, "accounts/setup.html", {"form": form})


class CronRackLoginView(LoginView):
    template_name = "accounts/login.html"
    form_class = LoginForm
    redirect_authenticated_user = True
