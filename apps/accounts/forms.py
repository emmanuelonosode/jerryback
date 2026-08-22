"""
Admin-facing auth forms.

The login screen labels its username field from the model's USERNAME_FIELD,
which here is `email_normalised` — an internal column name that exists so
uniqueness can be case-insensitive. Rendered as "Email normalised" it asks staff
to understand a storage detail in order to sign in. Overridden to say what the
person is actually being asked for.
"""

from django import forms
from unfold.forms import AuthenticationForm as UnfoldAuthenticationForm


class AdminLoginForm(UnfoldAuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Django's AuthenticationForm always names this field "username",
        # whatever USERNAME_FIELD is — only the *label* is derived from the
        # model. Looking it up by USERNAME_FIELD raises KeyError on POST.
        username = self.fields["username"]
        username.label = "Email address"
        username.widget = forms.EmailInput(attrs={
            **username.widget.attrs,
            "autocomplete": "username",
            "autofocus": True,
            "placeholder": "you@skeltonrealtygroup.com",
        })
        self.fields["password"].widget.attrs.setdefault("autocomplete", "current-password")
