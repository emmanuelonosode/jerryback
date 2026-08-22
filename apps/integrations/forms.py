from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from apps.accounts.models import User

from .message_templates import MESSAGE_TEMPLATES, TEMPLATE_CHOICES


class ComposeEmailForm(forms.Form):
    """
    Write one message and send it to whoever needs it.

    RECIPIENTS COME FROM EITHER SIDE, and both may be used at once. Staff
    writing to somebody who already has an account should not have to go and
    copy their address out of another screen, and staff writing to an applicant
    who has not registered must not be blocked because that person is not a
    User yet. Applicants apply as guests by design, so an address-only path is
    not an edge case here - it is most of the traffic.
    """

    template = forms.ChoiceField(
        choices=TEMPLATE_CHOICES,
        required=False,
        label="Start from a template",
        help_text="Fills the subject and message below. Edit them before sending.",
    )

    recipients = forms.ModelMultipleChoiceField(
        queryset=User.objects.order_by("email"),
        required=False,
        label="Existing accounts",
        widget=forms.SelectMultiple(attrs={"size": 10}),
        help_text="Hold Cmd or Ctrl to pick several.",
    )

    extra_emails = forms.CharField(
        required=False,
        label="Other email addresses",
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "one per line, or comma separated"}),
        help_text="For applicants who have not registered an account.",
    )

    subject = forms.CharField(max_length=300)

    body = forms.CharField(
        label="Message",
        widget=forms.Textarea(attrs={"rows": 16}),
        help_text=(
            "Plain text. The branded header, footer, licences and anti-fraud "
            "notice are added automatically — do not paste your own."
        ),
    )

    def clean_extra_emails(self):
        """One per line or comma separated, whichever staff happen to paste."""
        raw = self.cleaned_data.get("extra_emails", "")
        addresses, bad = [], []
        for chunk in raw.replace(",", "\n").splitlines():
            candidate = chunk.strip()
            if not candidate:
                continue
            try:
                validate_email(candidate)
            except ValidationError:
                bad.append(candidate)
                continue
            addresses.append(candidate)

        if bad:
            raise ValidationError(
                "These are not valid email addresses: " + ", ".join(bad[:5])
                + ("…" if len(bad) > 5 else "")
            )
        return addresses

    def clean(self):
        cleaned = super().clean()
        chosen = list(cleaned.get("recipients") or [])
        typed = list(cleaned.get("extra_emails") or [])

        if not chosen and not typed:
            raise ValidationError(
                "Pick at least one account or type at least one email address."
            )

        # De-duplicated case-insensitively, so picking an account AND typing the
        # same address does not send the person two copies.
        seen, addresses = set(), []
        for address in [user.email for user in chosen] + typed:
            key = address.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            addresses.append(address.strip())

        cleaned["all_addresses"] = addresses
        return cleaned


def template_payload(key: str) -> dict:
    """Subject and body for a template key, for the picker's JavaScript."""
    template = MESSAGE_TEMPLATES.get(key)
    if not template:
        return {"subject": "", "body": ""}
    return {"subject": template["subject"], "body": template["body"]}
