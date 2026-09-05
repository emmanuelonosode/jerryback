from django.urls import path

from . import views

urlpatterns = [
    path("request/", views.request_tour, name="request-tour"),
    # Optional, and addressed by the unguessable public id the request handed
    # back - there is no account at this point.
    path("<uuid:public_id>/id/", views.upload_tour_id, name="upload-tour-id"),
]
