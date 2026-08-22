from django.urls import path

from . import views

urlpatterns = [
    path("maintenance/", views.maintenance, name="portal-maintenance"),
    path("documents/", views.my_documents, name="portal-documents"),
]
