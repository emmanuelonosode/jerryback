import json

from django.contrib import admin, messages
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html
from unfold.admin import ModelAdmin as UnfoldModelAdmin

from .branding import render_email_html
from .forms import ComposeEmailForm
from .message_templates import MESSAGE_TEMPLATES
from .models import EmailStatus, OutboundEmail, queue_email


@admin.register(OutboundEmail)
class OutboundEmailAdmin(UnfoldModelAdmin):
    """
    The outbox.

    WHAT THIS SCREEN IS FOR. Two jobs that used to have no home at all: seeing
    whether a message actually went, and writing one. The model existed and was
    registered with `pass`, so the list showed Django's default repr and there
    was no way to send anything by hand - staff could see that mail was queued
    and had no way to act on it.

    Nothing here composes HTML. Every message, whether queued by the
    application flow or typed on the compose screen, goes through
    `queue_email`, which wraps it in the branded header and footer. That is the
    only reason a hand-written message cannot ship without the logo, the
    licence numbers, or the line telling the recipient we never send payment
    details by email.
    """

    list_display = ("subject", "to_email", "status_badge", "template", "created_at", "sent_at")
    list_filter = ("status", "template", "created_at")
    search_fields = ("to_email", "subject", "body_text")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    readonly_fields = (
        "to_email", "subject", "body_text", "body_html", "template", "status",
        "attempts", "last_error", "send_after", "sent_at", "created_at", "preview_link",
    )
    actions = ["requeue_selected"]

    # Composing is the reason to be here; the list is the record.
    change_list_template = "admin/integrations/outboundemail/change_list.html"

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        colour = {
            EmailStatus.SENT: "#138018",
            EmailStatus.QUEUED: "#0251A7",
            EmailStatus.SENDING: "#8A5A00",
            EmailStatus.FAILED: "#A32C2C",
        }.get(obj.status, "#6A6A6A")
        label = obj.get_status_display()
        if obj.status == EmailStatus.FAILED and obj.last_error:
            label = f"{label} — {obj.last_error[:60]}"
        return format_html('<b style="color:{}">{}</b>', colour, label)

    @admin.display(description="Preview")
    def preview_link(self, obj):
        if not obj.pk:
            return "—"
        url = reverse("admin:integrations_outboundemail_preview", args=[obj.pk])
        return format_html(
            '<a href="{}" target="_blank" rel="noopener">Open the rendered email</a>', url,
        )

    def has_add_permission(self, request):
        """
        Adding through the default form is disabled on purpose.

        That form would let someone save a row with an empty `body_html`, which
        sends as an unbranded wall of text. Compose is the supported path and
        it cannot produce one.
        """
        return False

    def get_urls(self):
        return [
            path(
                "compose/",
                self.admin_site.admin_view(self.compose_view),
                name="integrations_outboundemail_compose",
            ),
            path(
                "<uuid:pk>/preview/",
                self.admin_site.admin_view(self.preview_view),
                name="integrations_outboundemail_preview",
            ),
        ] + super().get_urls()

    def preview_view(self, request, pk):
        """
        Exactly what the recipient sees, rendered from the stored HTML.

        Re-rendering from the body text would show what the template produces
        *now*, not what was actually queued - so a wording change would quietly
        rewrite history on a message already sent.
        """
        email = self.get_object(request, pk)
        if email is None:
            messages.error(request, "That message no longer exists.")
            return redirect("admin:integrations_outboundemail_changelist")
        html = email.body_html or render_email_html(email.subject, email.body_text)
        return HttpResponse(html)

    def compose_view(self, request):
        if request.method == "POST":
            form = ComposeEmailForm(request.POST)
            if form.is_valid():
                addresses = form.cleaned_data["all_addresses"]
                subject = form.cleaned_data["subject"]
                body = form.cleaned_data["body"]

                # One row per recipient rather than one row with many. It keeps
                # per-address delivery state, so one bounce does not hide behind
                # four successes, and nobody sees who else was written to.
                for address in addresses:
                    queue_email(
                        to_email=address,
                        subject=subject,
                        body_text=body,
                        template=form.cleaned_data.get("template") or "manual",
                    )

                messages.success(
                    request,
                    f"Queued for {len(addresses)} recipient"
                    f"{'' if len(addresses) == 1 else 's'}. "
                    "It goes out on the next send_queued_email run.",
                )
                return redirect("admin:integrations_outboundemail_changelist")
        else:
            form = ComposeEmailForm()

        context = {
            **self.admin_site.each_context(request),
            "title": "Compose an email",
            "form": form,
            "opts": self.model._meta,
            # Feeds the picker so choosing a template fills the fields without
            # a round trip.
            "templates_json": json.dumps(
                {key: {"subject": value["subject"], "body": value["body"]}
                 for key, value in MESSAGE_TEMPLATES.items()}
            ),
        }
        return TemplateResponse(request, "admin/integrations/compose.html", context)

    @admin.action(description="Send again — put back in the queue")
    def requeue_selected(self, request, queryset):
        """
        For a message that failed, or one that needs sending a second time.

        Attempts reset to zero: a message that exhausted its retries against a
        broken mail server would otherwise stay failed forever after the server
        was fixed.
        """
        done = queryset.update(
            status=EmailStatus.QUEUED, attempts=0, last_error="", sent_at=None,
        )
        self.message_user(request, f"{done} message(s) queued again.")
