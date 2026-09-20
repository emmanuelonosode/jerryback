from django.urls import path

from . import views

urlpatterns = [
    path("apply/my-applications/", views.my_applications, name="my-applications"),
    path("my-applications/", views.my_applications, name="my-applications-direct"),
    path("apply/drafts/", views.create_draft, name="create-draft"),
    path("apply/drafts/<uuid:draft_id>/", views.draft_detail, name="draft-detail"),
    path("apply/drafts/<uuid:draft_id>/submit/", views.submit_draft, name="submit-draft"),
    path(
        "apply/drafts/<uuid:draft_id>/payment-methods/",
        views.draft_payment_methods,
        name="draft-payment-methods",
    ),
    path("contact/", views.contact_inquiry, name="contact-inquiry"),
    path("alerts/", views.alert_subscription, name="alert-subscription"),
    path("callback/", views.callback_request, name="callback-request"),
    path("lease/latest/", views.lease_agreement_latest, name="lease-agreement-latest"),
    path("lease/<uuid:application_id>/", views.lease_agreement_detail, name="lease-agreement-detail"),
    path("lease/<uuid:application_id>/sign/", views.sign_lease_agreement, name="sign-lease-agreement"),
]
