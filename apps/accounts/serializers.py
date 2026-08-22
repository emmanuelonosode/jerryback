from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from .models import User


class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, max_length=512)
    first_name = serializers.CharField(max_length=50)
    last_name = serializers.CharField(max_length=50)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)

    def validate_password(self, value):
        validate_password(value)
        return value


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, max_length=512)


class VerifyEmailSerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField(min_length=6, max_length=6)


class UserSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = (
            "id", "email", "first_name", "last_name", "full_name", "phone",
            "role", "is_email_verified", "onboarding_completed", "preferences",
        )
        # A client must never be able to promote itself by PATCHing its role.
        read_only_fields = ("id", "email", "role", "is_email_verified")

class ChangePasswordSerializer(serializers.Serializer):
    """
    Password change, which is an authenticated action that still proves identity.

    THE CURRENT PASSWORD IS REQUIRED even though the caller already holds a valid
    access token. A token can be replayed from an unlocked laptop or a stolen
    device; asking for the existing password is what stops a change of ownership
    being a thing a passer-by can do in ten seconds.

    The new password goes through Django's configured validators rather than a
    local rule, so the portal enforces exactly what registration does — a
    weaker check here would be the way in.
    """

    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)

    def validate_new_password(self, value):
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError as DjangoValidationError

        user = self.context.get("user")
        try:
            validate_password(value, user=user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages)) from exc
        return value

    def validate(self, attrs):
        user = self.context["user"]
        if not user.check_password(attrs["current_password"]):
            # Deliberately attached to the field, not raised as a generic error:
            # the caller is already authenticated, so there is no enumeration
            # risk here and a vague "invalid input" just wastes their time.
            raise serializers.ValidationError({"current_password": "That is not your current password."})
        if attrs["current_password"] == attrs["new_password"]:
            raise serializers.ValidationError({"new_password": "Choose a password you are not already using."})
        return attrs
