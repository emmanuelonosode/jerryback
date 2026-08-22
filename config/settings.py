"""
Django settings for the Skelton Realty Group admin/API service.

DEPLOYED AT admin.skeltonrealtygroup.com, SEPARATE FROM THE PUBLIC SITE.

That separation is the point, and it drives several settings below that would be
wrong for a single-origin app:

  Cookies are scoped to this host, NOT to `.skeltonrealtygroup.com`. A session
  cookie shared across the parent domain would be sent to the public marketing
  site on every request, so any XSS anywhere on the public site would hand over
  a staff session. Staff auth stays on this host.

  CORS is an allowlist of exact origins, never a wildcard, and credentials are
  only permitted for those. The public site reads published inventory from this
  API; it must not be able to drive the admin.

  CSRF trusted origins must be listed explicitly, because Django 4+ requires the
  scheme and this is a cross-subdomain deployment.

MONEY IS INTEGER CENTS, NOT DecimalField.

Python's Decimal is exact, so the usual argument for it does not apply — this is
about having one representation. The public site's money model is integer cents
end to end; if this API emitted "3345.00" the frontend would parse it back, and
that parsing boundary is exactly where money bugs live. Cents in the database,
cents on the wire, formatting only at the edge that renders.
"""

import sys
from pathlib import Path

import environ
from django.templatetags.static import static
from django.urls import reverse_lazy

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
    CORS_ALLOWED_ORIGINS=(list, []),
    CSRF_TRUSTED_ORIGINS=(list, []),
    SECURE_SSL_REDIRECT=(bool, True),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY", default="dev-only-insecure-key-change-me-min-50-chars-long!!")
DEBUG = env("DEBUG")

ALLOWED_HOSTS = env("ALLOWED_HOSTS") or (["*"] if DEBUG else [])

"""
LOOPBACK IS ALWAYS ALLOWED.

The public site renders server-side and calls this API over loopback, so those
requests arrive with `Host: 127.0.0.1:8000`. With ALLOWED_HOSTS set to the
public hostname and nothing else, Django answers 400 DisallowedHost - and the
frontend reports it as "Listing API unreachable", which reads like the API is
down when it is running perfectly. It also breaks container health checks for
the same reason.

Safe to add unconditionally: a loopback address is only reachable from the
machine itself, so this widens nothing an attacker can reach. It is appended
rather than replacing the configured list, so the public hostname still has to
be set explicitly.
"""
for _loopback in ("127.0.0.1", "localhost", "[::1]"):
    if _loopback not in ALLOWED_HOSTS and "*" not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(_loopback)

# --- Applications ------------------------------------------------------------

INSTALLED_APPS = [
    # Unfold MUST come before django.contrib.admin. It replaces the admin's
    # templates, and Django resolves templates in INSTALLED_APPS order — listed
    # after, the stock admin templates win and the theme silently does nothing.
    "unfold",
    "unfold.contrib.filters",
    "unfold.contrib.forms",
    "unfold.contrib.inlines",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "rest_framework",
    "corsheaders",
    "apps.accounts",
    "apps.properties",
    "apps.crm",
    "apps.billing",
    "apps.scheduler",
    "apps.portal",
    "apps.content",
    "apps.analytics",
    "apps.integrations",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    # Must precede CommonMiddleware so preflight requests get their headers.
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# --- Database ----------------------------------------------------------------
# SQLite for local work, Postgres in production via DATABASE_URL.

DATABASES = {
    "default": env.db_url(
        "DATABASE_URL",
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
    )
}

"""
ENGINE IS CHOSEN BY `DATABASE_URL`, NOT BY CODE.

SQLite is only the fallback so a checkout runs with nothing installed. Anything
else is a URL:

    postgresql://user:password@host:5432/srg_admin
    mysql://user:password@host:3306/srg_admin

Nothing in this project is Postgres-specific - no `django.contrib.postgres`
imports, no array or range fields, no full-text search vectors - so MySQL is a
supported target rather than a port. Two things it does require:

  * MySQL 8.0.16 or newer. Fourteen tables carry CHECK constraints, which
    older MySQL parses and then silently ignores. On 8.0.16+ they are enforced;
    below it they are decoration, and several of them are the only thing
    stopping a payment of zero or a fee that duplicates the rent.

  * utf8mb4, set below. The default collation is accent- and case-insensitive,
    which suits this data: the property search normalises both the stored text
    and the query to lowercase anyway, so it behaves identically on either
    engine.
"""
_engine = DATABASES["default"].get("ENGINE", "")

if "mysql" in _engine:
    # mysqlclient is the faster C driver but needs libmysqlclient present at
    # build time. PyMySQL is pure Python and needs nothing, so it stands in
    # when the C one is unavailable rather than failing at import.
    try:
        import MySQLdb  # noqa: F401
    except ImportError:  # pragma: no cover - depends on the host
        import pymysql

        pymysql.install_as_MySQLdb()

    DATABASES["default"].setdefault("OPTIONS", {})
    DATABASES["default"]["OPTIONS"].update({
        "charset": "utf8mb4",
        # STRICT_TRANS_TABLES turns silent truncation into an error. Without it
        # MySQL quietly shortens an over-long value and writes it anyway, which
        # is exactly the class of failure this codebase spends its constraints
        # trying to prevent.
        "sql_mode": "STRICT_TRANS_TABLES",
    })
DATABASES["default"]["ATOMIC_REQUESTS"] = True
"""
ATOMIC_REQUESTS is on deliberately.

Several operations here span multiple writes that must not half-apply: a
decision writes a status, an adverse action notice, and a move-in invoice;
verifying a payment writes the payment and possibly closes an invoice. Wrapping
each request in a transaction makes the default safe, so the failure mode of
forgetting `atomic()` is a slower request rather than a half-decided
application.
"""

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    # Eight, the floor NIST SP 800-63B sets for a user-chosen password, and the
    # length most people expect. Length is still the property that actually
    # helps - which is why the composition rules that push people toward
    # Password1! are deliberately absent - but the three validators either side
    # of this one carry more weight than the extra four characters did: a
    # password that is short is rejected here, and one that is common, numeric,
    # or built from the user's own name and email is rejected by those.
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 8},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Argon2 first, with the others retained so existing hashes still verify and are
# upgraded on next login.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

# --- Internationalisation ----------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# --- Static and media --------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# Without this our own static/ is never searched, and a file placed under
# static/admin/ silently loses to django.contrib.admin's copy of the same path.
# Assets therefore live under static/srg/ rather than shadowing a built-in.
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

MEDIA_URL = env("MEDIA_URL", default="/media/")
MEDIA_ROOT = env("MEDIA_ROOT", default=str(BASE_DIR / "media"))
# Absolute origin for media served to OTHER hosts — the public site runs on a
# different domain to this admin, so a relative /media/ path 404s there. Empty
# means "derive it from the request", which is what local development wants.
MEDIA_BASE_URL = env("MEDIA_BASE_URL", default="")

# Pluggable storage: local disk now, object storage later. Read by
# apps.core.storage rather than swapping Django's STORAGES wholesale, because
# image records keep both an ingest source and a served URL.
STORAGE_BACKEND = env("STORAGE_BACKEND", default="local")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Where the Django admin is mounted. Empty means the site root, which is right
# for a host that IS the admin — a bare hostname lands on the login page rather
# than a 404. Set ADMIN_PATH=staff/ in production to take this host out of the
# noise floor of bots probing /admin/. Not security on its own.
ADMIN_PATH = env("ADMIN_PATH", default="")

# --- REST framework ----------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.accounts.authentication.JWTAuthentication",
        # Session auth so the browsable API works while logged into the admin.
        "rest_framework.authentication.SessionAuthentication",
    ],
    # Closed by default. An endpoint is public only by saying so explicitly,
    # which is the right direction for a mistake to fail in.
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 24,
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "120/min",
        "user": "600/min",
        # Anything that can be brute-forced or that sends mail on demand.
        "auth": "10/min",
        "otp": "5/min",
        # Generous: one page view produces a small batch, and a visitor moving
        # quickly through listings is normal traffic, not abuse. The cap exists
        # so a runaway client cannot fill the spool, not to police browsing.
        "telemetry": "600/min",
        # A person books one or two tours, not twenty.
        "tour": "10/hour",
    },
}

# --- JWT ---------------------------------------------------------------------

JWT_SECRET = env("JWT_SECRET", default=SECRET_KEY)
JWT_ACCESS_TTL_SECONDS = env.int("JWT_ACCESS_TTL_SECONDS", default=4 * 60 * 60)
JWT_REFRESH_TTL_SECONDS = env.int("JWT_REFRESH_TTL_SECONDS", default=14 * 24 * 60 * 60)

# --- Cross-origin ------------------------------------------------------------

CORS_ALLOWED_ORIGINS = env("CORS_ALLOWED_ORIGINS") or (
    ["http://localhost:3210", "http://127.0.0.1:3210"] if DEBUG else []
)
# Never CORS_ALLOW_ALL_ORIGINS. With credentials enabled it lets any site drive
# this API using a logged-in staff member's browser.
CORS_ALLOW_CREDENTIALS = True

CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS") or (
    ["http://localhost:8000"] if DEBUG else ["https://admin.skeltonrealtygroup.com"]
)

# --- Security ----------------------------------------------------------------

SESSION_COOKIE_NAME = "srg_admin_sessionid"
CSRF_COOKIE_NAME = "srg_admin_csrftoken"
"""
Cookie names are prefixed and the domain is left unset on purpose.

Unset means the cookie is host-only: admin.skeltonrealtygroup.com and nowhere
else. Setting `.skeltonrealtygroup.com` would send staff session cookies to the
public marketing site on every request, so any XSS on the public site would
become a staff session takeover. The distinct names also stop a cookie from the
public site colliding with one here.
"""
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_COOKIE_AGE = 12 * 60 * 60  # A staff shift, not two weeks.

if not DEBUG:
    SECURE_SSL_REDIRECT = env("SECURE_SSL_REDIRECT")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    X_FRAME_OPTIONS = "DENY"
    # Behind a reverse proxy terminating TLS.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# --- Integrations ------------------------------------------------------------

MAILER_SYNC_KEY = env("MAILER_SYNC_KEY", default="")

DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="Skelton Realty Group <no-reply@skeltonrealtygroup.com>")
EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")

PUBLIC_SITE_URL = env("PUBLIC_SITE_URL", default="https://skeltonrealtygroup.com")

# --- Business policy ---------------------------------------------------------
# Real values are supplied by the business; nothing here is invented. The
# launch gate in the public repo blocks on these.

MOVE_IN_LEASE_ADMIN_FEE_CENTS = env.int("MOVE_IN_LEASE_ADMIN_FEE_CENTS", default=15000)
# ---------------------------------------------------------------------------
# Security deposit ceiling, by state.
#
# THIS IS LAW, NOT PREFERENCE, AND IT VARIES BY STATE. Several states cap the
# deposit at a multiple of one month's rent, and the multiple differs — so this
# is a per-state table the business and its counsel fill in, keyed by the
# two-letter code, with a fallback for states that are not listed.
#
# A deposit over the ceiling is REPORTED, never silently reduced. Quietly
# clamping hides a policy conflict from the person who has to resolve it, and
# clamping to a cap that may not apply in that jurisdiction is its own error.
# An empty table means "no ceiling configured", and the move-in breakdown says
# so rather than implying the figure has been checked.
SECURITY_DEPOSIT_MAX_MONTHS: dict[str, float] = {}
SECURITY_DEPOSIT_MAX_MONTHS_DEFAULT: float | None = None

APPLICATION_FEE_CENTS = env.int("APPLICATION_FEE_CENTS", default=5500)
DECISION_WINDOW_HOURS = env.int("DECISION_WINDOW_HOURS", default=24)

# --- Test configuration ------------------------------------------------------
# Detected rather than kept in a second settings module, so tests exercise the
# real configuration and only the two things that make testing impossible are
# changed.

TESTING = "test" in sys.argv

if TESTING or DEBUG:
    # Rates raised rather than removed: the throttle classes stay wired, so a
    # misconfigured scope still fails loudly instead of only in production.
    REST_FRAMEWORK = {
        **REST_FRAMEWORK,
        "DEFAULT_THROTTLE_RATES": {k: "100000/min" for k in REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]},
    }

if TESTING:
    # Otherwise every request 301s to https and no test can reach a view.
    SECURE_SSL_REDIRECT = False
    SECURE_HSTS_SECONDS = 0
    # Argon2 is correct in production and makes a suite crawl; the hashing
    # behaviour itself is covered by its own tests.
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "{levelname} {asctime} {name} {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
}


# =============================================================================
# UNFOLD — admin theme
#
# THE SIDEBAR IS GROUPED BY JOB, NOT BY DJANGO APP.
#
# The default admin lists models by the app that happens to contain them, which
# is an artefact of how the code is organised and not how anyone works. Staff
# here do one of four things: keep inventory truthful, move a lead toward an
# application, decide an application and take the money, or let someone into a
# house. The navigation below is those four things, so the person covering the
# 24-hour decision promise finds applications and payments next to each other
# rather than two apps apart.
#
# Several entries carry a live badge. That is deliberate: the counts that matter
# here are ones where a delay is a broken promise to a real person — an
# application past its deadline, a payment somebody has already sent and is
# waiting on, an identity document sitting unreviewed. A queue nobody can see is
# a queue nobody works.
# =============================================================================

def _badge_overdue_applications(request):
    """Applications past the publicly promised 24-hour decision window."""
    from apps.crm.models import ApplicationStatus, RentalApplication
    from django.utils import timezone

    return RentalApplication.objects.filter(
        decision_due_at__isnull=False, decided_at__isnull=True,
        decision_due_at__lt=timezone.now(),
    ).count() or None


def _badge_payments_awaiting(request):
    """Money someone has already sent, waiting on a person to confirm it."""
    from apps.billing.models import Payment, PaymentStatus

    return Payment.objects.filter(status=PaymentStatus.PENDING_VERIFICATION).count() or None


def _badge_tours_awaiting(request):
    """Identity documents uploaded and not yet reviewed."""
    from apps.scheduler.models import TourRequest, TourStatus

    return TourRequest.objects.filter(status=TourStatus.PENDING_REVIEW).count() or None


def _badge_new_leads(request):
    from apps.crm.models import Lead, LeadStatus

    return Lead.objects.filter(status=LeadStatus.NEW).count() or None


def _badge_open_maintenance(request):
    from apps.portal.models import MaintenanceRequest, MaintenanceStatus

    return MaintenanceRequest.objects.exclude(
        status__in=[MaintenanceStatus.RESOLVED, MaintenanceStatus.CLOSED],
    ).count() or None


def _staff_only(request):
    return request.user.is_authenticated and request.user.is_staff


def _admin_only(request):
    return request.user.is_authenticated and request.user.is_superuser


UNFOLD = {
    "DASHBOARD_CALLBACK": "config.dashboard.callback",
    # Unfold's Tailwind is precompiled, so utility classes it does not itself
    # use resolve to nothing. Dashboard styling ships as its own stylesheet
    # rather than as classes that silently do not exist.
    "STYLES": [lambda request: static("srg/css/dashboard.css")],
    "SITE_TITLE": "Skelton Realty Group",
    "SITE_HEADER": "Skelton Realty Group",
    "SITE_SUBHEADER": "Operations",
    "SITE_URL": PUBLIC_SITE_URL,
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": True,
    "SHOW_BACK_BUTTON": True,
    "ENVIRONMENT": "config.settings.environment_banner",
    "BORDER_RADIUS": "6px",
    "COLORS": {
        # Neutral is warm rather than pure grey, so the chrome sits closer to
        # the public site's palette than to a default admin.
        "base": {
            "50": "250 250 249", "100": "245 245 244", "200": "231 229 228",
            "300": "214 211 209", "400": "168 162 158", "500": "120 113 108",
            "600": "87 83 78", "700": "68 64 60", "800": "41 37 36",
            "900": "28 25 23", "950": "12 10 9",
        },
        # The brand blue. Used for navigation and primary actions only — never
        # for status, which has its own semantics.
        "primary": {
            "50": "239 245 252", "100": "216 229 245", "200": "175 200 233",
            "300": "127 166 218", "400": "74 128 200", "500": "29 93 184",
            "600": "23 75 150", "700": "18 56 110", "800": "13 42 83",
            "900": "8 28 56", "950": "5 18 36",
        },
        "font": {
            "subtle-light": "var(--color-base-500)",
            "subtle-dark": "var(--color-base-400)",
            "default-light": "var(--color-base-700)",
            "default-dark": "var(--color-base-200)",
            "important-light": "var(--color-base-900)",
            "important-dark": "var(--color-base-100)",
        },
    },
    "SIDEBAR": {
        "show_search": True,
        "show_all_applications": False,
        "navigation": [
            {
                # The dashboard had no way back to it from anywhere in the
                # admin — the only route was editing the URL by hand, which
                # meant the one screen that says what needs doing today was
                # effectively unreachable once you clicked into anything.
                "title": "Overview",
                "separator": False,
                "items": [
                    {
                        "title": "Operations",
                        "icon": "dashboard",
                        "link": lambda r: reverse_lazy("admin:index"),
                        "permission": _staff_only,
                    },
                ],
            },
            {
                "title": "Needs attention",
                "separator": False,
                "items": [
                    {
                        "title": "Applications",
                        "icon": "assignment",
                        "link": lambda r: reverse_lazy("admin:crm_rentalapplication_changelist"),
                        "badge": "config.settings._badge_overdue_applications",
                        "permission": _staff_only,
                    },
                    {
                        "title": "Payments to verify",
                        "icon": "price_check",
                        "link": lambda r: reverse_lazy("admin:billing_payment_changelist"),
                        "badge": "config.settings._badge_payments_awaiting",
                        "permission": _staff_only,
                    },
                    {
                        "title": "Tour IDs to review",
                        "icon": "badge",
                        "link": lambda r: reverse_lazy("admin:scheduler_tourrequest_changelist"),
                        "badge": "config.settings._badge_tours_awaiting",
                        "permission": _staff_only,
                    },
                    {
                        "title": "Open maintenance",
                        "icon": "handyman",
                        "link": lambda r: reverse_lazy("admin:portal_maintenancerequest_changelist"),
                        "badge": "config.settings._badge_open_maintenance",
                        "permission": _staff_only,
                    },
                ],
            },
            {
                "title": "Inventory",
                "separator": True,
                "items": [
                    {
                        "title": "Homes",
                        "icon": "home_work",
                        "link": lambda r: reverse_lazy("admin:properties_property_changelist"),
                        "permission": _staff_only,
                    },
                    {
                        "title": "Amenity categories",
                        "icon": "category",
                        "link": lambda r: reverse_lazy("admin:properties_amenitycategory_changelist"),
                        "permission": _staff_only,
                    },
                ],
            },
            {
                "title": "People",
                "separator": True,
                "items": [
                    {
                        "title": "Leads",
                        "icon": "person_search",
                        "link": lambda r: reverse_lazy("admin:crm_lead_changelist"),
                        "badge": "config.settings._badge_new_leads",
                        "permission": _staff_only,
                    },
                    {
                        "title": "Clients",
                        "icon": "groups",
                        "link": lambda r: reverse_lazy("admin:crm_client_changelist"),
                        "permission": _staff_only,
                    },
                    {
                        "title": "Viewings",
                        "icon": "event",
                        "link": lambda r: reverse_lazy("admin:scheduler_viewing_changelist"),
                        "permission": _staff_only,
                    },
                    {
                        "title": "Documents",
                        "icon": "folder_shared",
                        "link": lambda r: reverse_lazy("admin:portal_clientdocument_changelist"),
                        "permission": _staff_only,
                    },
                ],
            },
            {
                "title": "Money",
                "separator": True,
                "items": [
                    {
                        "title": "Invoices",
                        "icon": "receipt_long",
                        "link": lambda r: reverse_lazy("admin:billing_invoice_changelist"),
                        "permission": _staff_only,
                    },
                    {
                        "title": "Payment methods",
                        "icon": "account_balance",
                        "link": lambda r: reverse_lazy("admin:billing_paymentmethodconfig_changelist"),
                        "permission": _admin_only,
                    },
                    {
                        "title": "Referral payouts",
                        "icon": "handshake",
                        "link": lambda r: reverse_lazy("admin:crm_referralpayout_changelist"),
                        "permission": _staff_only,
                    },
                ],
            },
            {
                "title": "Compliance",
                "separator": True,
                "items": [
                    {
                        # Kept visible rather than buried: an unsent notice is a
                        # missed FCRA obligation, not an admin detail.
                        "title": "Adverse action notices",
                        "icon": "gavel",
                        "link": lambda r: reverse_lazy("admin:crm_adverseactionnotice_changelist"),
                        "permission": _staff_only,
                    },
                ],
            },
            {
                "title": "Content",
                "separator": True,
                "items": [
                    {
                        "title": "Guides",
                        "icon": "article",
                        "link": lambda r: reverse_lazy("admin:content_post_changelist"),
                        "permission": _staff_only,
                    },
                    {
                        "title": "Job applications",
                        "icon": "work",
                        "link": lambda r: reverse_lazy("admin:content_jobapplication_changelist"),
                        "permission": _staff_only,
                    },
                ],
            },
            {
                "title": "Administration",
                "separator": True,
                "items": [
                    {
                        "title": "Staff and users",
                        "icon": "manage_accounts",
                        "link": lambda r: reverse_lazy("admin:accounts_user_changelist"),
                        "permission": _admin_only,
                    },
                    {
                        "title": "Sessions",
                        "icon": "key",
                        "link": lambda r: reverse_lazy("admin:accounts_refreshtoken_changelist"),
                        "permission": _admin_only,
                    },
                    {
                        "title": "Outbound email",
                        "icon": "outgoing_mail",
                        "link": lambda r: reverse_lazy("admin:integrations_outboundemail_changelist"),
                        "permission": _admin_only,
                    },
                ],
            },
        ],
    },
}


def environment_banner(request):
    """
    A coloured banner naming the environment.

    Worth the few lines: this admin verifies real payments and decides real
    applications, and the single most expensive mistake available is doing that
    on production while believing you are on staging.
    """
    if DEBUG:
        return ["Local development", "warning"]
    host = request.get_host()
    if "staging" in host or "127.0.0.1" in host or "localhost" in host:
        return ["Staging", "warning"]
    return None
