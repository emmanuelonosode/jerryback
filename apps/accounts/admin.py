from django.contrib import admin
from unfold.admin import ModelAdmin as UnfoldModelAdmin, TabularInline as UnfoldTabularInline, StackedInline as UnfoldStackedInline
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
# Unfold's own auth forms, not Django's. Without these the add-user, change-user
# and password screens keep the stock widgets inside an otherwise themed shell,
# which reads as half-broken rather than as a deliberate look.
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm

from .models import AgentProfile, EmailVerificationCode, RefreshToken, User


class SrgUserCreationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("email", "first_name", "last_name", "role")


class SrgUserChangeForm(UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = User
        fields = "__all__"


@admin.register(User)
class UserAdmin(BaseUserAdmin, UnfoldModelAdmin):
    add_form = SrgUserCreationForm
    form = SrgUserChangeForm
    change_password_form = AdminPasswordChangeForm
    model = User

    # USERNAME_FIELD is email_normalised, so the default UserAdmin's
    # username-based ordering and fieldsets do not apply.
    ordering = ("-date_joined",)
    list_display = ("email", "full_name", "role", "is_email_verified", "is_active", "is_staff")
    list_filter = ("role", "is_active", "is_staff", "is_email_verified")
    search_fields = ("email", "first_name", "last_name", "phone")
    readonly_fields = ("email_normalised", "date_joined", "last_login", "updated_at")

    fieldsets = (
        (None, {"fields": ("email", "email_normalised", "password")}),
        ("Person", {"fields": ("first_name", "last_name", "phone", "avatar_url")}),
        ("Role and access", {
            "fields": ("role", "is_active", "is_staff", "is_superuser", "is_email_verified"),
            "description": (
                "`role` drives API permissions and is an explicit grant table, not a rank — "
                "an accountant verifies payments and cannot see applicant PII; an agent reads "
                "applicants and cannot touch money. `is_staff` separately controls access to "
                "this admin site."
            ),
        }),
        ("Groups", {"fields": ("groups", "user_permissions"), "classes": ("collapse",)}),
        ("Dates", {"fields": ("date_joined", "last_login", "updated_at")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "first_name", "last_name", "role", "password1", "password2"),
        }),
    )


@admin.register(RefreshToken)
class RefreshTokenAdmin(UnfoldModelAdmin):
    """
    Read-only. Tokens are stored hashed and exist for revocation and for
    investigating a suspected session compromise, not for editing.
    """

    list_display = ("user", "family_id", "created_at", "expires_at", "revoked_at")
    list_filter = ("revoked_at",)
    readonly_fields = tuple(f.name for f in RefreshToken._meta.fields)
    actions = ["revoke"]

    def has_add_permission(self, request):
        return False

    @admin.action(description="Revoke — signs the device out")
    def revoke(self, request, queryset):
        from django.utils import timezone

        count = queryset.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())
        self.message_user(request, f"{count} session(s) revoked.")


@admin.register(EmailVerificationCode)
class EmailVerificationCodeAdmin(UnfoldModelAdmin):
    """
    Read-only, and the code itself is never shown — only its hash exists.

    An admin screen listing live verification codes would be an account
    takeover tool for anyone with staff access or a leaked screenshot.
    """

    list_display = ("user", "attempts", "expires_at", "consumed_at", "created_at")
    readonly_fields = tuple(f.name for f in EmailVerificationCode._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(AgentProfile)
class AgentProfileAdmin(UnfoldModelAdmin):
    pass

