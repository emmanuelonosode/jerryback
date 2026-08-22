"""
Create a staff account.

`createsuperuser` is awkward here because USERNAME_FIELD is `email_normalised`,
so it prompts for the normalised form rather than the address a person actually
types. This wraps it: give an email and a role, and the normalised form is
derived.

It also sets the two flags that are easy to confuse. `role` drives API
permissions through the grant table; `is_staff` separately controls access to
this admin site. A MANAGER with is_staff=False can use the API and cannot open
the admin, which is a legitimate combination and not a mistake.
"""

import getpass

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import Role, User


class Command(BaseCommand):
    help = "Create a staff account for the admin site."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--first-name", required=True)
        parser.add_argument("--last-name", required=True)
        parser.add_argument("--role", default=Role.ADMIN, choices=Role.values)
        parser.add_argument(
            "--password",
            help="Omit to be prompted. Passing it on the command line puts it in your shell history.",
        )
        parser.add_argument(
            "--no-admin-access", action="store_true",
            help="Grant the role without access to this admin site.",
        )

    def handle(self, *args, **options):
        email = options["email"].strip()
        if User.objects.filter(email_normalised=email.lower()).exists():
            raise CommandError(f"{email} already has an account.")

        password = options.get("password") or getpass.getpass("Password: ")
        try:
            validate_password(password)
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc

        user = User.objects.create_user(
            email=email, password=password,
            first_name=options["first_name"], last_name=options["last_name"],
            role=options["role"],
            is_staff=not options["no_admin_access"],
            # A staff account created by an operator does not need to prove it
            # owns the address; the operator already knows.
            is_email_verified=True,
        )
        if options["role"] == Role.ADMIN and not options["no_admin_access"]:
            user.is_superuser = True
            user.save(update_fields=["is_superuser"])

        self.stdout.write(self.style.SUCCESS(
            f"Created {user.email} as {user.role}"
            f"{' (no admin access)' if options['no_admin_access'] else ''}."
        ))
