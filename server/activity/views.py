from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET

from activity.models import StatusEvent
from hosts.presenters import OwnerData, activity_entry

PAGE_SIZE = 25
MAX_PAGE = 10_000


def _page_number(request: HttpRequest) -> int:
    try:
        page = int(request.GET.get("page", "1"))
    except ValueError:
        return 1
    return min(max(page, 1), MAX_PAGE)


def activity_page(user, page: int) -> dict:
    """One page of the user's status changes, newest first: {items, next_page}."""
    now = timezone.now()
    data = OwnerData(user)
    start = (page - 1) * PAGE_SIZE
    # One extra row tells us whether a next page exists, without a COUNT query.
    events = list(
        StatusEvent.objects.filter(host__owner=user).select_related("host")[
            start : start + PAGE_SIZE + 1
        ]
    )
    has_next = len(events) > PAGE_SIZE
    return {
        "items": [activity_entry(e, now, data, with_day=True) for e in events[:PAGE_SIZE]],
        "next_page": page + 1 if has_next else None,
    }


@login_required
@require_GET
def activity_index(request: HttpRequest) -> HttpResponse:
    return render(
        request, "activity/index.html", activity_page(request.user, _page_number(request))
    )


@login_required
@require_GET
def activity_json(request: HttpRequest) -> JsonResponse:
    return JsonResponse(activity_page(request.user, _page_number(request)))
