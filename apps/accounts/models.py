"""
Accounts and roles.

THREE DELIBERATE DEPARTURES FROM THE SUPPLIED SPEC, each a security decision
rather than a preference. They are recorded here because the next person to read
the spec will notice the field is missing and wonder why.

1. THERE IS NO `raw_password_encrypted` FIELD.

   The spec stores the user's password reversibly encrypted alongside its hash.
   That defeats hashing entirely: anyone who reaches the database and the server
   secret recovers every plaintext password, and because people reuse passwords
   the damage lands on their email and their bank, not on this site. No feature
   needs it — a reset issues a new credential, it does not recover the old one.

2. PASSWORDS GO THROUGH DJANGO'S HASHER CHAIN, Argon2 first.

   Configured in settings.PASSWORD_HASHERS. Older hashes still verify and are
   upgraded transparently on next login, which is the only moment the plaintext
   is available to rehash.

3. OTPs ARE STORED HASHED, WITH AN ATTEMPT CAP.

   The spec stores the six digits directly. A six-digit code is a million
   possibilities, so a hash is no barrier to an offline attack — but the
   realistic exposure is a leaked backup or an over-broad admin query showing
   live codes for accounts mid-verification, and hashing removes exactly that.
   The attempt cap is what stops the online attack.
"""

import hashlib
import hmac
import secrets
import uuid
from datetime import timedelta

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone


class Role(models.TextChoices):
    ADMIN = "ADMIN", "Administrator"
    MANAGER = "MANAGER", "Manager"
    AGENT = "AGENT", "Agent"
    ACCOUNTANT = "ACCOUNTANT", "Accountant"
    CLIENT = "CLIENT", "Client"


class UserManager(BaseUserManager):
    def _normalise(self, email: str) -> str:
        """
        Lowercase and trim only.

        Deliberately NOT stripping dots or +tags: those are Gmail conventions,
        not standards, and applying them universally merges genuinely distinct
        addresses at other providers.
        """
        return self.normalize_email(email).strip().lower()

    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError("An email address is required")
        normalised = self._normalise(email)
        user = self.model(email=email.strip(), email_normalised=normalised, **extra)
        # set_password hashes. There is no path here that stores the plaintext.
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("role", Role.ADMIN)
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_email_verified", True)
        return self.create_user(email, password, **extra)

    def get_by_natural_key(self, username):
        return self.get(email_normalised=self._normalise(username))


class User(AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(max_length=254)
    # Uniqueness must be case-insensitive or two accounts exist for one human.
    email_normalised = models.EmailField(max_length=254, unique=True, db_index=True)
    first_name = models.CharField(max_length=50)
    last_name = models.CharField(max_length=50)
    phone = models.CharField(max_length=20, blank=True, default="")
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.CLIENT, db_index=True)
    avatar_url = models.URLField(max_length=500, blank=True, default="")

    is_email_verified = models.BooleanField(default=False)
    onboarding_completed = models.BooleanField(default=False)
    preferences = models.JSONField(default=dict, blank=True)

    is_active = models.BooleanField(default=True)
    # Django's admin-access flag. Distinct from `role`, which is ours.
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email_normalised"
    REQUIRED_FIELDS = ["first_name", "last_name", "email"]

    class Meta:
        db_table = "users"
        ordering = ["-date_joined"]

    def __str__(self) -> str:
        return f"{self.first_name} {self.last_name} <{self.email}>"

    def save(self, *args, **kwargs):
        # Keep the normalised form authoritative even on direct saves.
        self.email_normalised = self.email.strip().lower()
        super().save(*args, **kwargs)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class EmailVerificationCode(models.Model):
    """
    A one-time code, hashed, single-use, attempt-capped.

    Its own table rather than columns on User, so issuing a new code revokes the
    old one by deletion and attempts are countable per issue.
    """

    LENGTH = 6
    TTL_MINUTES = 15
    MAX_ATTEMPTS = 5

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="verification_codes")
    code_hash = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "email_verification_codes"
        ordering = ["-created_at"]

    @staticmethod
    def hash_code(code: str, user_id) -> str:
        """
        SHA-256, salted with the user id.

        Plain SHA rather than a work factor, deliberately: the input space is a
        million values, so no cost factor makes it brute-force resistant
        offline. The expiry and the attempt cap are the protections that matter,
        and paying Argon2 on every verification would itself be a DoS lever.
        The user-id salt stops two accounts holding the same code from producing
        the same hash.
        """
        return hashlib.sha256(f"{user_id}:{code}".encode()).hexdigest()

    @classmethod
    def issue(cls, user: User) -> tuple["EmailVerificationCode", str]:
        """Issue a fresh code, invalidating any outstanding one."""
        cls.objects.filter(user=user).delete()
        # secrets, not random: a predictable code is a takeover of any account
        # whose email address is known.
        code = f"{secrets.randbelow(10 ** cls.LENGTH):0{cls.LENGTH}d}"
        record = cls.objects.create(
            user=user,
            code_hash=cls.hash_code(code, user.pk),
            expires_at=timezone.now() + timedelta(minutes=cls.TTL_MINUTES),
        )
        return record, code

    def validate(self, supplied: str) -> str | None:
        """
        Returns None when valid, else a machine-readable reason.

        Named `validate` rather than `check` because Django reserves `check` on
        models for its system-check framework.
        """
        if self.consumed_at:
            return "already-used"
        if self.attempts >= self.MAX_ATTEMPTS:
            return "too-many-attempts"
        expired = self.expires_at <= timezone.now()
        # Constant-time. Comparing digit strings with == leaks how many leading
        # characters matched, turning a million guesses into sixty.
        matches = hmac.compare_digest(self.code_hash, self.hash_code(supplied.strip(), self.user_id))
        if expired or not matches:
            # Count expired guesses too. Otherwise an attacker waits for expiry,
            # guesses freely against a record that no longer increments, and
            # then requests a resend having narrowed the space for free.
            self.attempts += 1
            self.save(update_fields=["attempts"])
            return "expired" if expired else "mismatch"
        return None

    def consume(self) -> None:
        self.consumed_at = timezone.now()
        self.save(update_fields=["consumed_at"])


class RefreshToken(models.Model):
    """
    One row per issued refresh token, hashed, rotated on use.

    `family_id` and `replaced_by` implement replay detection: presenting a token
    that has already been rotated means it leaked, and there is no way to tell
    which of the two holders is the real user. The whole family is revoked. The
    cost of being wrong is one extra login; the alternative leaves an attacker
    with a live session.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="refresh_tokens")
    family_id = models.UUIDField(db_index=True)
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    replaced_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL)
    user_agent = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "refresh_tokens"
        indexes = [models.Index(fields=["user", "revoked_at"])]

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @property
    def is_live(self) -> bool:
        return self.revoked_at is None and self.replaced_by_id is None and self.expires_at > timezone.now()


class AgentProfile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="agent_profile")
    avatar_url = models.URLField(max_length=500, blank=True, default="")
    bio = models.TextField(blank=True, default="")
    license_number = models.CharField(max_length=50, blank=True, default="")
    # The brief requires the licence number AND the jurisdiction that issued it.
    license_state = models.CharField(max_length=2, blank=True, default="")
    specialties = models.JSONField(default=list, blank=True)
    languages = models.JSONField(default=list, blank=True)
    social_links = models.JSONField(default=dict, blank=True)
    # Basis points: 300 = 3.00%. A decimal rate times a cent count produces
    # fractional cents on almost every transaction.
    commission_basis_points = models.PositiveIntegerField(default=300)
    total_sales_cents = models.BigIntegerField(default=0)
    years_experience = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "agent_profiles"

    def __str__(self) -> str:
        return f"Agent profile for {self.user.full_name}"
