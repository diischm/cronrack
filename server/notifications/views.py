"""Settings → Notifications: presets, quiet hours, daily summary, sounds, and every rule.

Works as a plain form (Save key, redirect back with a flash message) and with the page's
autosave (fetch with Accept: application/json gets {ok, ...}). Every query is scoped to
the logged-in user; host and cluster choices are checked against the user's own.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from clusters.models import Cluster
from cronrack.http import wants_json
from hosts.humanize import user_zone
from hosts.models import Host
from notifications.config import apply_preset, load_config
from notifications.forms import GeneralForm, RuleValues, parse_rules
from notifications.models import NotificationRuleOverride
from notifications.rules import (
    COUNTS_AS_LABELS,
    LEVEL_LABELS,
    PRESET_LABELS,
    PRESETS,
    SPECS,
    specs_by_group,
)
from prefs.models import Prefs

# Open on first view; the rest (Kubernetes: 27 rules most people never touch) starts closed.
OPEN_GROUPS = ("Hosts", "Services & containers", "Network")


def _stored_limit(spec, limit: str) -> str:
    """The limit key to store: empty (the rule's default) unless another choice was picked."""
    if spec.limit is None or limit == spec.limit.default:
        return ""
    return limit


def _save(owner, preset: str, values: dict[str, RuleValues]) -> list[str]:
    """Store the rules that differ from the preset; drop the rest. Returns the changed IDs."""
    changed = [v for v in values.values() if v.differs_from(SPECS[v.rule_id], preset)]
    with transaction.atomic():
        NotificationRuleOverride.objects.filter(owner=owner).exclude(
            rule_id__in=[v.rule_id for v in changed]
        ).delete()
        for value in changed:
            spec = SPECS[value.rule_id]
            override, _ = NotificationRuleOverride.objects.update_or_create(
                owner=owner,
                rule_id=value.rule_id,
                defaults={
                    "level": value.level,
                    "counts_as": "" if value.counts_as == spec.counts_as else value.counts_as,
                    "limit": _stored_limit(spec, value.limit),
                    "applies_to": value.applies_to,
                },
            )
            override.full_clean()
            override.hosts.set(value.hosts)
            override.clusters.set(value.clusters)
    return sorted(v.rule_id for v in changed)


def _save_general(form: GeneralForm, was_enabled: bool) -> None:
    """`was_enabled`: the summary switch before this POST. It must be read before the form
    is validated, because validating copies the posted values onto the instance."""
    turned_on = form.cleaned_data["digest_enabled"] and not was_enabled
    saved = form.save(commit=False)
    if turned_on:
        # Turned on after today's summary time: start tomorrow, not right away.
        local = timezone.now().astimezone(user_zone(saved.timezone))
        if local.time() >= saved.digest_time:
            saved.digest_sent_on = local.date()
    saved.save()


def _rules_context(config, hosts, clusters) -> list[dict]:
    groups = []
    for group, specs in specs_by_group():
        rows = []
        for spec in specs:
            choice = config.choices.get(spec.id)
            level = config.level(spec.id)
            counts_as = config.counts_as(spec.id)
            applies_to = choice.applies_to if choice else "all"
            if spec.applies_to == "hosts":
                targets = [(h.pk, h.name, bool(choice) and h.pk in choice.hosts) for h in hosts]
            elif spec.applies_to == "clusters":
                targets = [
                    (c.pk, c.name, bool(choice) and c.pk in choice.clusters) for c in clusters
                ]
            else:
                targets = []
            rows.append(
                {
                    "spec": spec,
                    "slug": spec.id.replace(".", "-").replace("_", "-"),
                    "changed": config.is_changed(spec.id),
                    "levels": [(k, label, k == level) for k, label in LEVEL_LABELS.items()],
                    "counts": [(k, label, k == counts_as) for k, label in COUNTS_AS_LABELS.items()],
                    "limit_key": config.limit_key(spec.id),
                    "applies_to": applies_to,
                    "targets": targets,
                    "target_kind": spec.applies_to,
                }
            )
        groups.append(
            {
                "name": group,
                "slug": group.lower().replace(" & ", "-").replace(" ", "-"),
                "open": group in OPEN_GROUPS,
                "rows": rows,
            }
        )
    return groups


@login_required
@require_http_methods(["GET", "POST"])
def index(request: HttpRequest) -> HttpResponse:
    prefs, _ = Prefs.objects.get_or_create(owner=request.user)
    hosts = list(Host.objects.filter(owner=request.user).order_by("name", "pk"))
    clusters = list(Cluster.objects.filter(owner=request.user).order_by("name", "pk"))
    general = GeneralForm(instance=prefs)
    errors: list[str] = []
    status = 200

    if request.method == "POST":
        digest_was_enabled = prefs.digest_enabled
        general = GeneralForm(request.POST, instance=prefs)
        values, errors = parse_rules(
            request.POST,
            prefs.notify_preset,
            {h.pk for h in hosts},
            {c.pk for c in clusters},
        )
        if general.is_valid() and not errors:
            with transaction.atomic():
                _save_general(general, digest_was_enabled)
                changed = _save(request.user, prefs.notify_preset, values)
            if wants_json(request):
                return JsonResponse({"ok": True, "changed": changed})
            messages.success(request, "Notifications saved.")
            return redirect("notifications")
        errors = errors + [e for field in general.errors.values() for e in field]
        status = 400
        if wants_json(request):
            return JsonResponse({"ok": False, "error": errors[0]}, status=400)
        prefs.refresh_from_db()

    config = load_config(request.user.pk)
    return render(
        request,
        "prefs/notifications.html",
        {
            "settings_tab": "notifications",
            "general": general,
            "prefs": prefs,
            "errors": errors,
            "presets": [(key, *PRESET_LABELS[key], key == config.preset) for key in PRESETS],
            "groups": _rules_context(config, hosts, clusters),
        },
        status=status,
    )


@login_required
@require_POST
def preset(request: HttpRequest) -> HttpResponse:
    """Switch the preset; every per-rule notification override goes back to its default."""
    chosen = request.POST.get("preset", "")
    if chosen not in PRESETS:
        if wants_json(request):
            return JsonResponse({"ok": False, "error": "Unknown preset."}, status=400)
        return HttpResponse("Unknown preset.", status=400, content_type="text/plain")
    apply_preset(request.user, chosen)
    if wants_json(request):
        return JsonResponse({"ok": True, "preset": chosen})
    messages.success(request, f"{PRESET_LABELS[chosen][0]} applied.")
    return redirect("notifications")
