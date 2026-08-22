from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from . import jwt as jwt_codec
from .models import User


class JWTAuthentication(BaseAuthentication):
    """Bearer access tokens. Refresh tokens are rejected here by type."""

    keyword = "Bearer"

    def authenticate(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.startswith(f"{self.keyword} "):
            return None
        token = header[len(self.keyword) + 1:].strip()
        try:
            claims = jwt_codec.decode(token, "access")
        except jwt_codec.TokenError as exc:
            raise AuthenticationFailed(f"Invalid token: {exc.reason}") from exc

        try:
            user = User.objects.get(pk=claims["sub"], is_active=True)
        except (User.DoesNotExist, ValueError, KeyError) as exc:
            raise AuthenticationFailed("No such active account") from exc

        # The role is re-read from the database rather than trusted from the
        # token. A token minted before a demotion would otherwise keep its old
        # privileges for the whole four-hour lifetime.
        return (user, token)

    def authenticate_header(self, request):
        return self.keyword
