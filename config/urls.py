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


ADMIN_PATH = getattr(settings, "ADMIN_PATH", "")

urlpatterns = [
    path("healthz", health, name="health"),
    path("api/v1/auth/", include("apps.accounts.urls")),
    path("api/v1/properties/", include("apps.properties.urls")),
    path("api/v1/leads/", include("apps.crm.urls")),
    path("api/v1/billing/", include("apps.billing.urls")),
    path("api/v1/viewings/", include("apps.scheduler.urls")),
    path("api/v1/portal/", include("apps.portal.urls")),
    path("api/v1/careers/", include("apps.content.urls")),
    path("api/v1/analytics/", include("apps.analytics.urls")),
    path("api/v1/mailer/", include("apps.integrations.urls")),
]

# Ingested listing photography, in development only.
#
# ORDER IS LOAD-BEARING. ADMIN_PATH is "" on this host, so `admin.site.urls` is
# mounted at the root and matches every path underneath it — including
# /media/. Appended after the admin, this route is unreachable and every image
# 302s to the login page instead of loading. It must come first.
#
# In production nginx serves /media/ directly and this adds nothing:
# django.conf.urls.static returns an empty list unless DEBUG.
urlpatterns = static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT) + urlpatterns + [
    path(ADMIN_PATH, admin.site.urls),
]
