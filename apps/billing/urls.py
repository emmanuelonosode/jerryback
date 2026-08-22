from django.urls import path

from . import views

urlpatterns = [
    path("my-invoices/", views.my_invoices, name="my-invoices"),
    path("my-payments/", views.my_payments, name="my-payments"),
    path("my-payments/submit-proof/", views.submit_proof, name="submit-proof"),
    path("summary/", views.my_billing_summary, name="billing-summary"),
    path("payment-config/", views.payment_config, name="payment-config"),
    path("payment-config/status/", views.payment_config_status, name="payment-config-status"),
]
