from django.urls import path

from . import views

urlpatterns = [
    path("register/", views.register, name="register"),
    path("verify-email/", views.verify_email, name="verify-email"),
    path("resend-otp/", views.resend_otp, name="resend-otp"),
    path("login/", views.login, name="login"),
    path("refresh/", views.refresh, name="refresh"),
    path("me/", views.me, name="me"),
    path("change-password/", views.change_password, name="change-password"),
    path("sign-out-everywhere/", views.sign_out_everywhere, name="sign-out-everywhere"),
]
