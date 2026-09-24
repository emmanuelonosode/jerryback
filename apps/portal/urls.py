from django.urls import path

from . import views, views_applicant

urlpatterns = [
    path("maintenance/", views.maintenance, name="portal-maintenance"),
    path("documents/", views.my_documents, name="portal-documents"),
    path("document-requests/", views_applicant.document_requests, name="portal-document-requests"),
    path(
        "document-requests/<uuid:request_id>/upload/",
        views_applicant.upload_for_request,
        name="portal-document-upload",
    ),
    path("guarantor/", views_applicant.guarantor, name="portal-guarantor"),
]
