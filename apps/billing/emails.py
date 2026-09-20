"""
Email delivery for customer invoices with payment instructions and direct CTA.
"""

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from .models import Invoice, PaymentMethodConfig


def send_invoice_email(invoice: Invoice) -> bool:
    """
    Dispatches a formal invoice notification to the resident or applicant,
    complete with itemized breakdown, CTA link to pay, and manual payment details.
    """
    recipient_email = None
    recipient_name = "Resident"

    if invoice.user and invoice.user.email:
        recipient_email = invoice.user.email
        name_val = getattr(invoice.user, "full_name", "") or (invoice.user.get_full_name() if hasattr(invoice.user, "get_full_name") else "")
        recipient_name = name_val.strip() if name_val and name_val.strip() else invoice.user.email.split("@")[0]
    elif invoice.rental_application and invoice.rental_application.email:
        recipient_email = invoice.rental_application.email
        first = invoice.rental_application.first_name
        last = invoice.rental_application.last_name
        recipient_name = f"{first} {last}".strip() or invoice.rental_application.email.split("@")[0]

    if not recipient_email:
        return False

    site_url = getattr(settings, "PUBLIC_SITE_URL", "https://skeltonrealtygroup.com").rstrip("/")
    payment_cta_url = f"{site_url}/portal/payments"

    # Format line items
    items_text = []
    for item in (invoice.line_items or []):
        desc = item.get("description") or "Charge"
        qty = int(item.get("quantity") or 1)
        total_cents = item.get("total_cents") or 0
        qty_str = f" (x{qty})" if qty > 1 else ""
        items_text.append(f"  - {desc}{qty_str}: ${total_cents / 100:,.2f}")

    items_block = "\n".join(items_text) if items_text else f"  - {invoice.title}: ${invoice.total_cents / 100:,.2f}"

    due_str = invoice.due_date.strftime("%B %d, %Y") if invoice.due_date else "Upon receipt"
    total_str = f"${invoice.total_cents / 100:,.2f}"

    # Build active manual payment methods
    active_methods = PaymentMethodConfig.objects.filter(is_active=True).order_by("display_name")
    manual_sections = []

    for method in active_methods:
        lines = [f"[{method.display_name}]"]
        if method.handle:
            lines.append(f"  Handle / Account ID: {method.handle}")
        if method.recipient_name:
            lines.append(f"  Recipient Name: {method.recipient_name}")
        if method.bank_name:
            lines.append(f"  Bank Name: {method.bank_name}")
        if method.account_number:
            lines.append(f"  Account Number: {method.account_number}")
        if method.routing_number:
            lines.append(f"  Routing Number: {method.routing_number}")
        if method.account_type:
            lines.append(f"  Account Type: {method.account_type}")
        if method.extra_instructions:
            lines.append(f"  Notes: {method.extra_instructions}")
        manual_sections.append("\n".join(lines))

    manual_methods_block = "\n\n".join(manual_sections) if manual_sections else "  Contact office for payment rails."

    subject = f"Invoice {invoice.invoice_number} from Skelton Realty Group - {invoice.title}"

    body = f"""Dear {recipient_name},

Your invoice {invoice.invoice_number} ({invoice.title}) has been issued by Skelton Realty Group and is now ready for payment.

INVOICE SUMMARY:
----------------------------------------
Invoice Number: {invoice.invoice_number}
Title:          {invoice.title}
Due Date:       {due_str}
Total Amount:   {total_str}

ITEMIZED BREAKDOWN:
{items_block}

Total Due:      {total_str}
----------------------------------------

PAYMENT INSTRUCTIONS & CALL TO ACTION:

1. PAY ONLINE VIA RESIDENT PORTAL:
Click the link below to review your invoice, select your preferred payment rail, and submit payment confirmation:
{payment_cta_url}

2. MANUAL PAYMENT METHODS:
You may also submit payment directly through any of our authorized business rails below.
IMPORTANT: Please include Invoice Number "{invoice.invoice_number}" in your payment memo or transfer reference.

{manual_methods_block}

3. UPLOAD PAYMENT PROOF:
Once payment is completed, please visit the Resident Portal ({payment_cta_url}) and upload your transfer receipt or confirmation screenshot so our accounting department can verify and credit your account immediately.

If you have any questions regarding this invoice, please contact accounting@skeltonrealtygroup.com or call (800) 555-0198.

Sincerely,
Skelton Realty Group Billing & Accounts Department
https://skeltonrealtygroup.com
"""

    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "info@skeltonrealtygroup.com")

    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=from_email,
            recipient_list=[recipient_email],
            fail_silently=False,
        )
        return True
    except Exception as exc:
        # Fall back to queue_email if available
        try:
            from apps.integrations.models import queue_email
            queue_email(
                to_email=recipient_email,
                subject=subject,
                body_text=body,
                send_now=False,
            )
            return True
        except Exception:
            return False
