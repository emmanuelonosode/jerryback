"""
Security tests for accounts.

These attack the implementation rather than exercising it. A security module
tested only on its happy path proves nothing.
"""

import time

from django.core.exceptions import ValidationError
from django.contrib.auth.password_validation import validate_password
from django.test import TestCase, override_settings
from django.utils import timezone

from . import jwt as jwt_codec
from .models import EmailVerificationCode, RefreshToken, Role, User
from .permissions import (
    APPLICATION_DECIDE, APPLICATION_READ_PII, CONFIG_WRITE, INVOICE_WRITE,
    PAYMENT_VERIFY, ROLE_PERMISSIONS, USER_WRITE, VIEWING_WRITE, can, is_staff_role,
)

TEST_SECRET = "a-test-jwt-secret-that-is-long-enough-32"


class UserModelTests(TestCase):
    def test_email_uniqueness_is_case_and_whitespace_insensitive(self):
        User.objects.create_user("Renter@Example.com", "correct horse battery", first_name="A", last_name="B")
        with self.assertRaises(Exception):
            User.objects.create_user("  renter@example.COM ", "another password!", first_name="C", last_name="D")

    def test_normalisation_does_not_merge_distinct_providers(self):
        # Stripping dots and +tags is a Gmail convention, not a standard.
        # Applying it universally merges genuinely different people.
        user = User.objects.create_user(
            "First.Last+tag@Example.com", "correct horse battery", first_name="A", last_name="B",
        )
        self.assertEqual(user.email_normalised, "first.last+tag@example.com")

    def test_password_is_only_ever_stored_hashed(self):
        # The spec asked for raw_password_encrypted alongside the hash. Anyone
        # with the database and the server secret would recover every plaintext.
        secret = "correct horse battery staple"
        user = User.objects.create_user("a@b.com", secret, first_name="A", last_name="B")
        columns = {f.name for f in User._meta.get_fields() if hasattr(f, "attname")}
        self.assertFalse(any("raw_password" in c or "password_plain" in c for c in columns))
        self.assertNotIn(secret, user.password)
        self.assertTrue(user.check_password(secret))

    def test_minimum_password_length_is_enforced(self):
        with self.assertRaises(ValidationError):
            validate_password("short")
        validate_password("a-sufficiently-long-passphrase")


class OtpTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("a@b.com", "correct horse battery", first_name="A", last_name="B")

    def test_codes_are_six_digits_and_unpredictable(self):
        codes = set()
        for _ in range(200):
            _, code = EmailVerificationCode.issue(self.user)
            self.assertRegex(code, r"^\d{6}$")
            codes.add(code)
        self.assertGreater(len(codes), 190)

    def test_hash_is_salted_per_user(self):
        # Otherwise two accounts holding the same code have the same hash,
        # visible to anyone who can read the table.
        other = User.objects.create_user("c@d.com", "correct horse battery", first_name="C", last_name="D")
        self.assertNotEqual(
            EmailVerificationCode.hash_code("123456", self.user.pk),
            EmailVerificationCode.hash_code("123456", other.pk),
        )

    def test_plaintext_code_is_never_stored(self):
        record, code = EmailVerificationCode.issue(self.user)
        self.assertNotEqual(record.code_hash, code)
        self.assertEqual(len(record.code_hash), 64)

    def test_right_code_passes_and_wrong_one_does_not(self):
        record, code = EmailVerificationCode.issue(self.user)
        self.assertIsNone(record.validate(code))
        self.assertEqual(record.validate("000000"), "mismatch")

    def test_attempt_cap_stops_online_brute_force(self):
        # A million possibilities falls in minutes without a cap. Note the
        # correct code is refused too: the code is burned, not just the guess.
        record, code = EmailVerificationCode.issue(self.user)
        for _ in range(EmailVerificationCode.MAX_ATTEMPTS):
            record.validate("000000")
        self.assertEqual(record.validate(code), "too-many-attempts")

    def test_expired_guesses_are_counted_too(self):
        # Otherwise an attacker waits for expiry, guesses freely against a
        # record that no longer increments, then requests a resend having
        # narrowed the space for free.
        record, _ = EmailVerificationCode.issue(self.user)
        record.expires_at = timezone.now() - timezone.timedelta(seconds=1)
        record.save(update_fields=["expires_at"])
        self.assertEqual(record.validate("000000"), "expired")
        record.refresh_from_db()
        self.assertEqual(record.attempts, 1)

    def test_a_consumed_code_cannot_be_replayed(self):
        record, code = EmailVerificationCode.issue(self.user)
        record.consume()
        self.assertEqual(record.validate(code), "already-used")

    def test_issuing_a_new_code_revokes_the_old_one(self):
        first, _ = EmailVerificationCode.issue(self.user)
        EmailVerificationCode.issue(self.user)
        self.assertFalse(EmailVerificationCode.objects.filter(pk=first.pk).exists())


@override_settings(JWT_SECRET=TEST_SECRET)
class JwtTests(TestCase):
    def test_round_trip(self):
        token = jwt_codec.encode(subject="u1", role="AGENT", token_type="access")
        claims = jwt_codec.decode(token, "access")
        self.assertEqual(claims["sub"], "u1")
        self.assertEqual(claims["role"], "AGENT")

    def test_alg_none_attack_is_rejected(self):
        # A verifier that reads alg from the header and obeys it accepts this.
        import base64, json

        def b64(obj):
            return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

        header = b64({"alg": "none", "typ": "JWT"})
        payload = b64({
            "sub": "attacker", "typ": "access", "role": "ADMIN",
            "iat": 1, "exp": 9_999_999_999, "jti": "x",
        })
        with self.assertRaises(jwt_codec.TokenError) as ctx:
            jwt_codec.decode(f"{header}.{payload}.", "access")
        self.assertEqual(ctx.exception.reason, "bad-algorithm")

    def test_algorithm_swap_to_rs256_is_rejected(self):
        # The other half: offer an HMAC verifier a token claiming asymmetric
        # signing, hoping a public key gets used as the secret.
        import base64, hmac, json
        from hashlib import sha256

        def b64(obj):
            return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

        header = b64({"alg": "RS256", "typ": "JWT"})
        payload = b64({"sub": "a", "typ": "access", "role": "ADMIN", "iat": 1, "exp": 9_999_999_999, "jti": "x"})
        sig = base64.urlsafe_b64encode(
            hmac.new(TEST_SECRET.encode(), f"{header}.{payload}".encode(), sha256).digest()
        ).rstrip(b"=").decode()
        with self.assertRaises(jwt_codec.TokenError) as ctx:
            jwt_codec.decode(f"{header}.{payload}.{sig}", "access")
        self.assertEqual(ctx.exception.reason, "bad-algorithm")

    def test_tampered_payload_is_rejected(self):
        import base64, json

        token = jwt_codec.encode(subject="u1", role="CLIENT", token_type="access")
        header, _, signature = token.split(".")
        escalated = base64.urlsafe_b64encode(
            json.dumps({"sub": "u1", "typ": "access", "role": "ADMIN", "iat": 1, "exp": 9_999_999_999, "jti": "x"}).encode()
        ).rstrip(b"=").decode()
        with self.assertRaises(jwt_codec.TokenError) as ctx:
            jwt_codec.decode(f"{header}.{escalated}.{signature}", "access")
        self.assertEqual(ctx.exception.reason, "bad-signature")

    def test_refresh_token_is_not_accepted_as_an_access_token(self):
        # Without a checked type claim the 14-day refresh token works wherever
        # the 4-hour access token does, and the session is silently 14 days.
        refresh = jwt_codec.encode(subject="u1", role="ADMIN", token_type="refresh", family="f")
        with self.assertRaises(jwt_codec.TokenError) as ctx:
            jwt_codec.decode(refresh, "access")
        self.assertEqual(ctx.exception.reason, "wrong-type")

    def test_expired_token_is_rejected(self):
        past = int(time.time()) - 60 * 60 * 24
        token = jwt_codec.encode(subject="u1", role="CLIENT", token_type="access", now=past)
        with self.assertRaises(jwt_codec.TokenError) as ctx:
            jwt_codec.decode(token, "access")
        self.assertEqual(ctx.exception.reason, "expired")

    def test_token_signed_with_another_secret_is_rejected(self):
        token = jwt_codec.encode(subject="u1", role="ADMIN", token_type="access")
        with override_settings(JWT_SECRET="a-completely-different-secret-abcdefgh"):
            with self.assertRaises(jwt_codec.TokenError):
                jwt_codec.decode(token, "access")

    def test_garbage_does_not_crash(self):
        for junk in ["", "a", "a.b", "a.b.c.d", "...", "ø.ø.ø"]:
            with self.assertRaises(jwt_codec.TokenError):
                jwt_codec.decode(junk, "access")

    @override_settings(JWT_SECRET="tooshort")
    def test_short_secret_is_refused(self):
        with self.assertRaises(jwt_codec.TokenError):
            jwt_codec.encode(subject="u", role="ADMIN", token_type="access")


class RbacTests(TestCase):
    def test_admin_holds_everything_and_client_holds_nothing(self):
        for permission in ROLE_PERMISSIONS[Role.ADMIN]:
            self.assertTrue(can(Role.ADMIN, permission), permission)
            self.assertFalse(can(Role.CLIENT, permission), permission)
        self.assertFalse(is_staff_role(Role.CLIENT))

    def test_agent_and_accountant_are_different_not_ranked(self):
        # The reason this is a grant table and not a hierarchy: each can do
        # something the other cannot, so no ordering of the two is correct.
        self.assertTrue(can(Role.AGENT, VIEWING_WRITE))
        self.assertFalse(can(Role.ACCOUNTANT, VIEWING_WRITE))
        self.assertTrue(can(Role.ACCOUNTANT, PAYMENT_VERIFY))
        self.assertFalse(can(Role.AGENT, PAYMENT_VERIFY))

    def test_only_admin_reads_application_pii(self):
        # Deciding needs income and rental history. It does not need a date of
        # birth or a licence number.
        for role in (Role.MANAGER, Role.AGENT, Role.ACCOUNTANT, Role.CLIENT):
            self.assertFalse(can(role, APPLICATION_READ_PII), role)
        self.assertTrue(can(Role.MANAGER, APPLICATION_DECIDE))

    def test_agent_cannot_touch_money_and_accountant_cannot_decide(self):
        self.assertFalse(can(Role.AGENT, INVOICE_WRITE))
        self.assertFalse(can(Role.ACCOUNTANT, APPLICATION_DECIDE))

    def test_only_admin_writes_users_or_config(self):
        for role in (Role.MANAGER, Role.AGENT, Role.ACCOUNTANT, Role.CLIENT):
            self.assertFalse(can(role, CONFIG_WRITE), role)
            self.assertFalse(can(role, USER_WRITE), role)

    def test_every_declared_role_has_an_entry(self):
        # So a new role cannot be added and default to open.
        for role in Role.values:
            self.assertIn(role, ROLE_PERMISSIONS)


@override_settings(JWT_SECRET=TEST_SECRET)
class RefreshTokenTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            "a@b.com", "correct horse battery", first_name="A", last_name="B", is_email_verified=True,
        )

    def test_token_is_stored_hashed(self):
        token = jwt_codec.encode(subject=str(self.user.pk), role=self.user.role, token_type="refresh", family="f")
        record = RefreshToken.objects.create(
            user=self.user, family_id=self.user.pk, token_hash=RefreshToken.hash_token(token),
            expires_at=timezone.now() + timezone.timedelta(days=14),
        )
        self.assertNotEqual(record.token_hash, token)
        self.assertRegex(record.token_hash, r"^[0-9a-f]{64}$")

    def test_is_live_reflects_revocation_and_expiry(self):
        record = RefreshToken.objects.create(
            user=self.user, family_id=self.user.pk, token_hash="x" * 64,
            expires_at=timezone.now() + timezone.timedelta(days=14),
        )
        self.assertTrue(record.is_live)
        record.revoked_at = timezone.now()
        self.assertFalse(record.is_live)
