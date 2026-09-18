from django.urls import path

from . import views
from . import views_admin

urlpatterns = [
    path("request/", views.request_tour, name="request-tour"),
    # Optional, and addressed by the unguessable public id the request handed
    # back - there is no account at this point.
    path("<uuid:public_id>/id/", views.upload_tour_id, name="upload-tour-id"),
    # Secure viewer for admin panel (both with and without trailing slash)
    path("admin/id/<str:filename>/", views_admin.secure_tour_id_view, name="secure-tour-id-view"),
    path("admin/id/<str:filename>", views_admin.secure_tour_id_view),
]
