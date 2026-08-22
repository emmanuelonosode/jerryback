from django.urls import path

from . import views

urlpatterns = [
    path("applications/", views.job_applications, name="job-applications"),
    path(
        "applications/<uuid:application_id>/",
        views.job_application_detail,
        name="job-application-detail",
    ),
    path("apply/", views.submit_job_application, name="submit-job-application"),
]
