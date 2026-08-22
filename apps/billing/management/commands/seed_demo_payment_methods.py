from django.core.management.base import BaseCommand

from apps.billing.models import CRYPTO_KINDS, IRREVERSIBLE_KINDS, PaymentMethodConfig, PaymentMethodKind


class Command(BaseCommand):
    """
    Fill every rail with obviously-fake details so the payment page can be tested.

    THE VALUES ARE DELIBERATELY NOT PLAUSIBLE. Reserved 555 numbers, a .test
    domain, all-zero routing numbers, wallet addresses that decode to nothing.
    A realistic-looking demo account number is one deploy away from being the
    number a real applicant sends rent to, and on a site whose whole position
    is "we are the real one", that is the worst possible bug. Anything seeded
    here has to be replaced by hand in the admin before launch.

        python manage.py seed_demo_payment_methods
        python manage.py seed_demo_payment_methods --deactivate
    """

    help = "Create demo payment method configs for testing the payment page."

    DEMOS = {
        PaymentMethodKind.ACH: dict(
            display_name="ACH transfer", recipient_name="Skelton Realty Group LLC",
            bank_name="Demo National Bank", account_type="Checking",
            account_number="000000123456", routing_number="000000000",
            clearing_time="1-3 business days",
        ),
        PaymentMethodKind.WIRE: dict(
            display_name="Wire transfer", recipient_name="Skelton Realty Group LLC",
            bank_name="Demo National Bank", account_type="Checking",
            account_number="000000123456", routing_number="000000000",
            clearing_time="Same day if sent before 2pm",
            extra_instructions="Your bank may charge a wire fee. That fee is theirs, not ours.",
        ),
        PaymentMethodKind.DIRECT_DEPOSIT: dict(
            display_name="Direct deposit", recipient_name="Skelton Realty Group LLC",
            bank_name="Demo National Bank", account_type="Checking",
            account_number="000000123456", routing_number="000000000",
            clearing_time="1-2 business days",
            extra_instructions="Pay in at any branch. Keep the counter receipt.",
        ),
        PaymentMethodKind.CHECK: dict(
            display_name="Check or money order", recipient_name="Skelton Realty Group LLC",
            handle="4445 Corporation Ln, Virginia Beach, VA 23462",
            clearing_time="5-7 days from posting",
            extra_instructions="Write your reference on the memo line.",
        ),
        PaymentMethodKind.ZELLE: dict(
            display_name="Zelle", handle="demo-payments@skeltonrealtygroup.test",
            clearing_time="Usually within minutes",
        ),
        PaymentMethodKind.VENMO: dict(
            display_name="Venmo", handle="@SkeltonRealty-Demo",
            clearing_time="Usually within minutes",
            extra_instructions="Send as a payment to a business, not as a personal transfer.",
        ),
        PaymentMethodKind.CASHAPP: dict(
            display_name="Cash App", handle="$SkeltonRealtyDemo",
            clearing_time="Usually within minutes",
        ),
        PaymentMethodKind.CHIME: dict(
            display_name="Chime", handle="$SkeltonRealtyDemo",
            clearing_time="Usually within minutes",
        ),
        PaymentMethodKind.PAYPAL: dict(
            display_name="PayPal", handle="demo-payments@skeltonrealtygroup.test",
            clearing_time="Usually within minutes",
            extra_instructions="Send as goods and services so you keep buyer protection.",
        ),
        PaymentMethodKind.APPLE_PAY: dict(
            display_name="Apple Pay", handle="(757) 555-0100",
            clearing_time="Usually within minutes",
            extra_instructions="Send through Messages to the number above.",
        ),
        PaymentMethodKind.LITECOIN: dict(
            display_name="Litecoin", handle="ltc1qdemo0000000000000000000000000000000000",
            clearing_time="15-30 minutes, after network confirmations",
            extra_instructions=(
                "Litecoin network only. The dollar amount is fixed at the rate when we "
                "receive it, so send promptly after checking the total."
            ),
        ),
        PaymentMethodKind.SOLANA: dict(
            display_name="Solana", handle="SoLDemo00000000000000000000000000000000000",
            clearing_time="Under a minute, after network confirmations",
            extra_instructions=(
                "Solana network only, and send USDC or SOL as agreed. Sending on another "
                "network loses the funds permanently."
            ),
        ),
    }

    def add_arguments(self, parser):
        parser.add_argument("--deactivate", action="store_true",
                            help="Turn every demo method off without deleting it.")

    def handle(self, *args, **options):
        if options["deactivate"]:
            changed = PaymentMethodConfig.objects.filter(is_active=True).update(is_active=False)
            self.stdout.write(self.style.SUCCESS(f"Deactivated {changed} method(s)."))
            return

        created = updated = 0
        for kind, fields in self.DEMOS.items():
            defaults = {
                "is_active": True,
                "irreversible": kind in IRREVERSIBLE_KINDS,
                **fields,
            }
            _, was_created = PaymentMethodConfig.objects.update_or_create(
                method=kind, defaults=defaults,
            )
            created += was_created
            updated += not was_created

        self.stdout.write(self.style.SUCCESS(f"Seeded {created} new, updated {updated}."))
        self.stdout.write(self.style.WARNING(
            "\n  THESE ARE FAKE. Reserved 555 numbers, a .test domain, all-zero routing\n"
            "  numbers and wallet addresses that decode to nothing. Replace every one in\n"
            "  the admin before launch, or applicants will send money nowhere.\n"
        ))
        crypto = ", ".join(k.label for k in CRYPTO_KINDS)
        self.stdout.write(self.style.WARNING(
            f"  {crypto} are irreversible AND the dollar value moves between sending and\n"
            "  confirmation. Decide who absorbs a shortfall before taking rent this way.\n"
        ))
