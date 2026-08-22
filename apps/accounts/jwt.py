"""
JWT signing and verification, HS256, on hashlib/hmac.

A library would be a few lines less and one more dependency. The risks of
hand-rolling are well known and specific, so each is handled explicitly rather
than left to a reader's trust:

  THE `alg: none` ATTACK. A verifier that reads the algorithm out of the token
  header and does what it says can be handed {"alg":"none"} and told to accept
  an unsigned token. `decode` never reads alg to decide anything — it requires
  exactly HS256 and rejects everything else, which also covers the other half of
  the attack (offering an HMAC verifier an RS256 token so a public key gets used
  as the secret).

  TIMING. Signature comparison is hmac.compare_digest.

  CLAIM CONFUSION. An access token and a refresh token are both JWTs. Without a
  checked type claim, a refresh token works wherever an access token does and
  the effective session length silently becomes fourteen days.
"""

import base64
import hmac
import json
import time
import uuid
from hashlib import sha256

from django.conf import settings


class TokenError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(payload: str) -> bytes:
    return hmac.new(settings.JWT_SECRET.encode(), payload.encode(), sha256).digest()


def encode(*, subject: str, role: str, token_type: str, family: str | None = None,
           now: int | None = None) -> str:
    if len(settings.JWT_SECRET) < 32:
        raise TokenError("JWT_SECRET must be at least 32 characters")
    issued = now if now is not None else int(time.time())
    ttl = settings.JWT_ACCESS_TTL_SECONDS if token_type == "access" else settings.JWT_REFRESH_TTL_SECONDS
    claims = {
        "sub": str(subject), "typ": token_type, "role": role,
        "iat": issued, "exp": issued + ttl, "jti": str(uuid.uuid4()),
    }
    if family:
        claims["fam"] = str(family)
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    return f"{header}.{body}.{_b64(_sign(f'{header}.{body}'))}"


def decode(token: str, expected_type: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenError("malformed")
    header_part, body_part, signature_part = parts

    try:
        header = json.loads(_unb64(header_part))
        claims = json.loads(_unb64(body_part))
    except Exception as exc:
        raise TokenError("malformed") from exc

    # Pinned, not read-and-obeyed. This is the whole alg:none defence.
    if header.get("alg") != "HS256":
        raise TokenError("bad-algorithm")

    if not hmac.compare_digest(_unb64(signature_part), _sign(f"{header_part}.{body_part}")):
        raise TokenError("bad-signature")

    # Checked only after the signature, so an unsigned token never reaches a
    # code path that trusts any of its claims.
    if claims.get("typ") != expected_type:
        raise TokenError("wrong-type")
    if not isinstance(claims.get("exp"), int) or claims["exp"] <= int(time.time()):
        raise TokenError("expired")
    return claims
