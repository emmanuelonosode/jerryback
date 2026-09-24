"""
URL routing.

The Django admin is the staff interface for this subdomain, so it lives at the
root rather than at /admin/ — this host IS the admin. That also means a stray
request to `/` lands on the login page instead of a 404, which is the right
behaviour for someone typing the bare hostname.

`ADMIN_PATH` allows moving it in production. Not security by itself, but it
removes this host from the noise floor of bots probing /admin/.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from apps.accounts.forms import AdminLoginForm

# Branding lives in settings.UNFOLD; these remain for anything that reads the
# stock attributes (error pages, password-reset emails).
admin.site.site_header = "Skelton Realty Group"
admin.site.site_title = "Skelton Realty Group admin"
admin.site.index_title = "Operations"

# Unfold themes the login screen through its own authentication form.
admin.site.login_form = AdminLoginForm


def health(_request):
    """Liveness probe. No database access, so it stays up during a migration."""
    return JsonResponse({"status": "ok"})


from django.views.generic.base import RedirectView

ADMIN_PATH = getattr(settings, "ADMIN_PATH", "")
admin_prefix = ADMIN_PATH.strip("/") + "/" if ADMIN_PATH else ""

redirect_patterns = []

# 1. Specific aliases first
redirect_patterns.extend([
    path("transactions/payment/", RedirectView.as_view(url=f"/{admin_prefix}billing/payment/", permanent=False, query_string=True)),
    path("transactions/payment", RedirectView.as_view(url=f"/{admin_prefix}billing/payment/", permanent=False, query_string=True)),
    path("admin/transactions/payment/", RedirectView.as_view(url=f"/{admin_prefix}billing/payment/", permanent=False, query_string=True)),
    path("admin/transactions/payment", RedirectView.as_view(url=f"/{admin_prefix}billing/payment/", permanent=False, query_string=True)),
])
if admin_prefix:
    redirect_patterns.extend([
        path(f"{admin_prefix}transactions/payment/", RedirectView.as_view(url=f"/{admin_prefix}billing/payment/", permanent=False, query_string=True)),
        path(f"{admin_prefix}transactions/payment", RedirectView.as_view(url=f"/{admin_prefix}billing/payment/", permanent=False, query_string=True)),
    ])

# 2. General root and admin redirects
if admin_prefix:
    redirect_patterns.append(
        path("", RedirectView.as_view(url=f"/{admin_prefix}", permanent=False))
    )
    if admin_prefix != "admin/":
        redirect_patterns.extend([
            path("admin/", RedirectView.as_view(url=f"/{admin_prefix}", permanent=False, query_string=True)),
            path("admin/<path:subpath>", RedirectView.as_view(url=f"/{admin_prefix}%(subpath)s", permanent=False, query_string=True)),
        ])

urlpatterns = [
    path("healthz", health, name="health"),
    path("api/v1/auth/", include("apps.accounts.urls")),
    path("api/v1/properties/", include("apps.properties.urls")),
    path("api/v1/leads/", include("apps.crm.urls")),
    path("api/v1/crm/", include("apps.crm.urls")),
    path("api/v1/apply/", include("apps.crm.urls")),
    path("api/v1/billing/", include("apps.billing.urls")),
    path("api/v1/viewings/", include("apps.scheduler.urls")),
    path("api/v1/portal/", include("apps.portal.urls")),
    path("api/v1/careers/", include("apps.content.urls")),
    path("api/v1/analytics/", include("apps.analytics.urls")),
    path("api/v1/mailer/", include("apps.integrations.urls")),
    path("api/v1/voice/", include("apps.voice.urls")),
]

urlpatterns = static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT) + redirect_patterns + urlpatterns + [
    path(ADMIN_PATH, admin.site.urls),
]

# --- Monkey-patch for Property Foreign Keys ---
# Loading all 7000+ properties in a dropdown causes massive timeouts.
# This forces all ModelAdmins with a Property ForeignKey to use autocomplete_fields.
from apps.properties.models import Property
for model, model_admin in admin.site._registry.items():
    has_prop = False
    for field in model._meta.get_fields():
        if field.name == 'property' and field.is_relation and field.related_model == Property:
            has_prop = True
            break
            
    if has_prop:
        ac_fields = list(getattr(model_admin, 'autocomplete_fields', []))
        if 'property' not in ac_fields:
            ac_fields.append('property')
            model_admin.autocomplete_fields = ac_fields
