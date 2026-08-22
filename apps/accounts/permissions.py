"""
Roles and permissions.

PERMISSIONS ARE ENUMERATED; ROLES ARE NOT A HIERARCHY.

The tempting model is a rank — ADMIN > MANAGER > AGENT > ACCOUNTANT > CLIENT —
with a `role >= required` check. It reads well and it is wrong here, because
ACCOUNTANT and AGENT are not more or less privileged than one another; they are
privileged over different things. An accountant should verify a payment and
never see an applicant's date of birth. An agent should read the applicant and
never mark money received. A rank forces one of those to be wrong.

So: an explicit grant table. Adding a permission to a role is a visible edit to
this file, which is the property that matters for something auditable.
"""

from rest_framework.permissions import BasePermission

from .models import Role

# Inventory
PROPERTY_READ = "property:read"
PROPERTY_WRITE = "property:write"
PROPERTY_PUBLISH = "property:publish"
# CRM
LEAD_READ = "lead:read"
LEAD_WRITE = "lead:write"
LEAD_ASSIGN = "lead:assign"
# Applications
APPLICATION_READ = "application:read"
APPLICATION_DECIDE = "application:decide"
# Separated from reading the application itself: deciding needs income and
# rental history; it does not need a date of birth or a licence number, and
# most staff who can decide should not see those.
APPLICATION_READ_PII = "application:read-pii"
# Money
INVOICE_READ = "invoice:read"
INVOICE_WRITE = "invoice:write"
PAYMENT_READ = "payment:read"
PAYMENT_VERIFY = "payment:verify"
# Scheduling
VIEWING_READ = "viewing:read"
VIEWING_WRITE = "viewing:write"
TOUR_REVIEW = "tour:review"
# Tenant-facing
MAINTENANCE_READ = "maintenance:read"
MAINTENANCE_MANAGE = "maintenance:manage"
DOCUMENT_READ = "document:read"
DOCUMENT_WRITE = "document:write"
# Hiring
HIRING_READ = "hiring:read"
HIRING_MANAGE = "hiring:manage"
# Content and administration
CONTENT_WRITE = "content:write"
USER_READ = "user:read"
USER_WRITE = "user:write"
CONFIG_WRITE = "config:write"
ANALYTICS_READ = "analytics:read"

ALL_PERMISSIONS = frozenset({
    PROPERTY_READ, PROPERTY_WRITE, PROPERTY_PUBLISH,
    LEAD_READ, LEAD_WRITE, LEAD_ASSIGN,
    APPLICATION_READ, APPLICATION_DECIDE, APPLICATION_READ_PII,
    INVOICE_READ, INVOICE_WRITE, PAYMENT_READ, PAYMENT_VERIFY,
    VIEWING_READ, VIEWING_WRITE, TOUR_REVIEW,
    HIRING_READ, HIRING_MANAGE,
    MAINTENANCE_READ, MAINTENANCE_MANAGE, DOCUMENT_READ, DOCUMENT_WRITE,
    CONTENT_WRITE, USER_READ, USER_WRITE, CONFIG_WRITE, ANALYTICS_READ,
})

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    Role.ADMIN: ALL_PERMISSIONS,
    Role.MANAGER: frozenset({
        PROPERTY_READ, PROPERTY_WRITE, PROPERTY_PUBLISH,
        LEAD_READ, LEAD_WRITE, LEAD_ASSIGN,
        APPLICATION_READ, APPLICATION_DECIDE,
        INVOICE_READ, INVOICE_WRITE, PAYMENT_READ,
        VIEWING_READ, VIEWING_WRITE, TOUR_REVIEW,
        HIRING_READ, HIRING_MANAGE,
        MAINTENANCE_READ, MAINTENANCE_MANAGE, DOCUMENT_READ, DOCUMENT_WRITE,
        CONTENT_WRITE, USER_READ, ANALYTICS_READ,
    }),
    Role.AGENT: frozenset({
        PROPERTY_READ,
        LEAD_READ, LEAD_WRITE,
        APPLICATION_READ,
        VIEWING_READ, VIEWING_WRITE, TOUR_REVIEW,
        MAINTENANCE_READ, DOCUMENT_READ,
    }),
    Role.ACCOUNTANT: frozenset({
        PROPERTY_READ,
        APPLICATION_READ,
        INVOICE_READ, INVOICE_WRITE,
        PAYMENT_READ, PAYMENT_VERIFY,
        ANALYTICS_READ,
    }),
    # A client holds no staff permission at all; their access is scoped by
    # ownership, which is a different question — see IsOwnerOrHasPermission.
    Role.CLIENT: frozenset(),
}


def can(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


def is_staff_role(role: str) -> bool:
    """
    Derived from holding any permission rather than from a list of role names,
    so a new role cannot be added and accidentally treated as public.
    """
    return bool(ROLE_PERMISSIONS.get(role))


class HasPermission(BasePermission):
    """Usage: `permission_classes = [HasPermission.of(PAYMENT_VERIFY)]`."""

    required = None

    @classmethod
    def of(cls, permission: str):
        return type(f"HasPermission_{permission}", (cls,), {"required": permission})

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        return can(user.role, self.required)


class IsOwnerOrHasPermission(BasePermission):
    """
    Ownership and permission are different questions, and conflating them is how
    one user reads another's application.

    A CLIENT may read *their own* record without holding the staff permission;
    holding the staff permission does not require owning anything.
    """

    required = None
    owner_field = "user_id"

    @classmethod
    def of(cls, permission: str, owner_field: str = "user_id"):
        return type(
            f"IsOwnerOr_{permission}",
            (cls,),
            {"required": permission, "owner_field": owner_field},
        )

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        if can(request.user.role, self.required):
            return True
        owner_id = getattr(obj, self.owner_field, None)
        return owner_id is not None and str(owner_id) == str(request.user.pk)
