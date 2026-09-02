"""
Authentication endpoints.

REGISTRATION DOES NOT REVEAL WHETHER AN EMAIL EXISTS. Returning "already
registered" turns the endpoint into an account-enumeration oracle: an attacker
learns which addresses have accounts, which is useful for phishing and for
credential stuffing. The response is identical either way, and the existing
account is told by email instead.
"""

import uuid

from django.contrib.auth import authenticate
from django.db import transaction
from django.utils import timezone
from datetime import timedelta
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from django.conf import settings

from . import jwt as jwt_codec
from .models import EmailVerificationCode, RefreshToken, User
from .permissions import ROLE_PERMISSIONS
from .serializers import (
    ChangePasswordSerializer,
    LoginSerializer,
    RegisterSerializer,
    UserSerializer,
    VerifyEmailSerializer,
)


class AuthThrottle(ScopedRateThrottle):
    scope = "auth"


class OtpThrottle(ScopedRateThrottle):
    scope = "otp"


def _issue_tokens(user, family_id=None, user_agent=""):
    family_id = family_id or uuid.uuid4()
    access = jwt_codec.encode(subject=str(user.pk), role=user.role, token_type="access")
    refresh = jwt_codec.encode(
        subject=str(user.pk), role=user.role, token_type="refresh", family=str(family_id),
    )
    RefreshToken.objects.create(
        user=user, family_id=family_id, token_hash=RefreshToken.hash_token(refresh),
        expires_at=timezone.now() + timedelta(seconds=settings.JWT_REFRESH_TTL_SECONDS),
        user_agent=user_agent[:300],
    )
    return {"access": access, "refresh": refresh}


def _queue_otp_email(user, code):
    # Through `queue_email`, not straight to the model: that is the one entry
    # point that builds the branded HTML, so anything queued around it arrives
    # as an unstyled wall of text with no footer, no licence numbers and none
    # of the anti-fraud wording. A verification code is the first email a new
    # applicant ever gets from us, and the one most likely to be spoofed.
    from apps.integrations.models import queue_email

    # Queued, never sent on the request thread: an SMTP timeout here would turn
    # a 200 into a 504 after the account was already created.
    queue_email(
        # Sent before this returns. Somebody is looking at a "check your email"
        # screen with the code field already focused; waiting on the next cron
        # tick is the difference between finishing a registration and giving up.
        # If the send fails the row stays queued and the retry picks it up.
        send_now=True,
        to_email=user.email,
        subject="Your Skelton Realty Group verification code",
        body_text=(
            f"Your verification code is {code}.\n\n"
            f"It expires in {EmailVerificationCode.TTL_MINUTES} minutes.\n\n"
            # Was followed by "we will never ask you to read this code out to
            # anyone, and nobody from Skelton Realty Group will ever phone you
            # for it" - a caution about impersonation, in a one-line email
            # somebody is reading to get on with signing in. Keep the code
            # private, said once and warmly.
            "Keep it to yourself, and if anything looks off just reply to this "
            "email - we are happy to help."
        ),
        template="otp",
    )


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([AuthThrottle])
def register(request):
    serializer = RegisterSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    normalised = data["email"].strip().lower()

    with transaction.atomic():
        if not User.objects.filter(email_normalised=normalised).exists():
            user = User.objects.create_user(
                email=data["email"], password=data["password"],
                first_name=data["first_name"], last_name=data["last_name"],
                phone=data.get("phone", ""),
            )
            _, code = EmailVerificationCode.issue(user)
            _queue_otp_email(user, code)

    # Identical response either way. See the module docstring.
    return Response(
        {"detail": "If that address can be registered, a verification code is on its way."},
        status=status.HTTP_202_ACCEPTED,
    )


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([OtpThrottle])
def verify_email(request):
    serializer = VerifyEmailSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    user = User.objects.filter(email_normalised=data["email"].strip().lower()).first()
    record = user.verification_codes.first() if user else None
    if not user or not record:
        return Response({"detail": "That code is not valid."}, status=status.HTTP_400_BAD_REQUEST)

    reason = record.validate(data["code"])
    if reason:
        return Response({"detail": "That code is not valid.", "reason": reason},
                        status=status.HTTP_400_BAD_REQUEST)

    record.consume()
    user.is_email_verified = True
    user.save(update_fields=["is_email_verified", "updated_at"])

    # Any application they made as a guest becomes theirs now that the address
    # is proven, so it appears in their portal instead of being invisible to
    # the person who filled it in.
    from apps.crm.services import link_applications_to_user

    link_applications_to_user(user)

    return Response({
        "user": UserSerializer(user).data,
        "tokens": _issue_tokens(user, user_agent=request.META.get("HTTP_USER_AGENT", "")),
    })


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([OtpThrottle])
def resend_otp(request):
    email = (request.data.get("email") or "").strip().lower()
    user = User.objects.filter(email_normalised=email, is_email_verified=False).first()
    if user:
        _, code = EmailVerificationCode.issue(user)
        _queue_otp_email(user, code)
    # Same response whether or not the account exists.
    return Response({"detail": "If that address needs verifying, a new code is on its way."})


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([AuthThrottle])
def login(request):
    serializer = LoginSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    # Django's authenticate runs the hasher even for a missing account, so a
    # wrong password and an unknown address take comparable time.
    user = authenticate(
        request, username=data["email"].strip().lower(), password=data["password"],
    )
    if user is None:
        return Response({"detail": "Those details do not match."}, status=status.HTTP_401_UNAUTHORIZED)
    if not user.is_email_verified:
        return Response({"detail": "Verify your email address first.", "reason": "unverified"},
                        status=status.HTTP_403_FORBIDDEN)

    # Catches applications made between sign-ups, and accounts verified before
    # this linking existed.
    from apps.crm.services import link_applications_to_user

    link_applications_to_user(user)

    return Response({
        "user": UserSerializer(user).data,
        "tokens": _issue_tokens(user, user_agent=request.META.get("HTTP_USER_AGENT", "")),
    })


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([AuthThrottle])
def refresh(request):
    """
    Rotate a refresh token.

    REPLAY REVOKES THE WHOLE FAMILY. A token presented after it was already
    exchanged means two parties hold it, and there is no way to tell which is
    the real user. Being wrong costs one login; the alternative leaves an
    attacker with a live session.
    """
    token = request.data.get("refresh") or ""
    try:
        jwt_codec.decode(token, "refresh")
    except jwt_codec.TokenError:
        return Response({"detail": "Invalid refresh token."}, status=status.HTTP_401_UNAUTHORIZED)

    record = RefreshToken.objects.filter(token_hash=RefreshToken.hash_token(token)).first()
    if record is None:
        return Response({"detail": "Invalid refresh token."}, status=status.HTTP_401_UNAUTHORIZED)

    if record.revoked_at or record.replaced_by_id:
        RefreshToken.objects.filter(family_id=record.family_id, revoked_at__isnull=True).update(
            revoked_at=timezone.now(),
        )
        return Response(
            {"detail": "That session has been signed out for safety. Please sign in again.",
             "reason": "replayed"},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    if record.expires_at <= timezone.now() or not record.user.is_active:
        return Response({"detail": "Invalid refresh token."}, status=status.HTTP_401_UNAUTHORIZED)

    tokens = _issue_tokens(record.user, record.family_id, request.META.get("HTTP_USER_AGENT", ""))
    issued = RefreshToken.objects.get(token_hash=RefreshToken.hash_token(tokens["refresh"]))
    record.revoked_at = timezone.now()
    record.replaced_by = issued
    record.save(update_fields=["revoked_at", "replaced_by"])
    return Response({"tokens": tokens})


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def me(request):
    if request.method == "PATCH":
        serializer = UserSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)
    return Response({
        **UserSerializer(request.user).data,
        # The client should not have to hard-code the grant table.
        "permissions": sorted(ROLE_PERMISSIONS.get(request.user.role, [])),
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def sign_out_everywhere(request):
    count = RefreshToken.objects.filter(user=request.user, revoked_at__isnull=True).update(
        revoked_at=timezone.now(),
    )
    return Response({"revoked": count})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def change_password(request):
    """
    Change the password, then invalidate every refresh token the account holds.

    REVOKING SESSIONS IS THE POINT, not a courtesy. The usual reason someone
    changes a password is that they think somebody else has it; leaving the
    other party's refresh token live means the change accomplished nothing —
    they simply mint a new access token and carry on. The caller's own other
    devices are signed out too, which is the correct trade.
    """
    serializer = ChangePasswordSerializer(data=request.data, context={"user": request.user})
    serializer.is_valid(raise_exception=True)

    with transaction.atomic():
        request.user.set_password(serializer.validated_data["new_password"])
        request.user.save(update_fields=["password"])
        revoked = RefreshToken.objects.filter(
            user=request.user, revoked_at__isnull=True,
        ).update(revoked_at=timezone.now())

    return Response({"detail": "Password changed.", "sessions_revoked": revoked})
