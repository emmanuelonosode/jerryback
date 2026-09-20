"""
Email delivery for customer invoices with payment instructions and direct CTA.
"""

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, send_mail
from django.utils import timezone

from .models import Invoice, PaymentMethodConfig


def _build_invoice_html(invoice: Invoice, recipient_name: str, payment_cta_url: str) -> str:
    """
    Renders an executive-grade HTML email for an invoice using Skelton Realty Group branding.
    Structured for maximum compatibility across mobile and desktop email clients (Gmail, Apple Mail, Outlook).
    """
    due_str = invoice.due_date.strftime("%B %d, %Y") if invoice.due_date else "Upon receipt"
    issued_str = invoice.issued_date.strftime("%B %d, %Y") if invoice.issued_date else timezone.now().strftime("%B %d, %Y")
    total_str = f"${invoice.total_cents / 100:,.2f}"

    # Line item rows
    item_rows = []
    for item in (invoice.line_items or []):
        desc = item.get("description") or "Charge"
        qty = int(item.get("quantity") or 1)
        total_cents = item.get("total_cents") or 0
        amount_str = f"${total_cents / 100:,.2f}"
        item_rows.append(f"""
        <tr>
          <td style="padding: 12px 14px; border-bottom: 1px solid #e2e8f0; font-size: 14px; color: #1e293b;">{desc}</td>
          <td style="padding: 12px 14px; border-bottom: 1px solid #e2e8f0; font-size: 14px; color: #475569; text-align: center;">{qty}</td>
          <td style="padding: 12px 14px; border-bottom: 1px solid #e2e8f0; font-size: 14px; color: #0f172a; text-align: right; font-weight: 600;">{amount_str}</td>
        </tr>
        """)

    if not item_rows:
        item_rows.append(f"""
        <tr>
          <td style="padding: 12px 14px; border-bottom: 1px solid #e2e8f0; font-size: 14px; color: #1e293b;">{invoice.title}</td>
          <td style="padding: 12px 14px; border-bottom: 1px solid #e2e8f0; font-size: 14px; color: #475569; text-align: center;">1</td>
          <td style="padding: 12px 14px; border-bottom: 1px solid #e2e8f0; font-size: 14px; color: #0f172a; text-align: right; font-weight: 600;">{total_str}</td>
        </tr>
        """)

    items_html = "".join(item_rows)

    # Active payment methods cards
    active_methods = PaymentMethodConfig.objects.filter(is_active=True).order_by("display_name")
    method_cards = []

    for method in active_methods:
        kind_label = method.get_method_display()
        title_label = method.display_name if (method.display_name and method.display_name != kind_label) else kind_label
        details = []

        if method.handle:
            details.append(f"<div><strong style='color: #475569;'>Account / Handle:</strong> <span style='font-family: monospace; font-size: 13px; color: #0f172a; font-weight: 600;'>{method.handle}</span></div>")
        if method.recipient_name:
            details.append(f"<div><strong style='color: #475569;'>Recipient Name:</strong> <span style='color: #0f172a;'>{method.recipient_name}</span></div>")
        if method.bank_name:
            details.append(f"<div><strong style='color: #475569;'>Bank:</strong> <span style='color: #0f172a;'>{method.bank_name}</span></div>")
        if method.routing_number:
            details.append(f"<div><strong style='color: #475569;'>Routing Number:</strong> <span style='font-family: monospace; font-size: 13px; color: #0f172a; font-weight: 600;'>{method.routing_number}</span></div>")
        if method.account_number:
            details.append(f"<div><strong style='color: #475569;'>Account Number:</strong> <span style='font-family: monospace; font-size: 13px; color: #0f172a; font-weight: 600;'>{method.account_number}</span></div>")
        if method.account_type:
            details.append(f"<div><strong style='color: #475569;'>Account Type:</strong> <span style='color: #0f172a;'>{method.account_type}</span></div>")
        if method.extra_instructions:
            details.append(f"<div style='margin-top: 4px; font-size: 12px; color: #64748b;'><em>Note: {method.extra_instructions}</em></div>")

        details_html = "".join(f"<div style='margin-bottom: 4px; font-size: 13px;'>{d}</div>" for d in details)

        method_cards.append(f"""
        <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 14px 16px; margin-bottom: 12px;">
          <div style="font-weight: 700; font-size: 14px; color: #064e3b; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px;">
            {title_label}
          </div>
          {details_html}
        </div>
        """)

    payment_rails_html = "".join(method_cards) if method_cards else "<p style='font-size: 13px; color: #64748b;'>Please contact the leasing office for payment instructions.</p>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Invoice {invoice.invoice_number} from Skelton Realty Group</title>
</head>
<body style="margin: 0; padding: 0; background-color: #f1f5f9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #1e293b; -webkit-font-smoothing: antialiased;">
  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color: #f1f5f9; padding: 30px 10px;">
    <tr>
      <td align="center">
        <!-- Main Card Container -->
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width: 620px; background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 2px 4px -2px rgba(0, 0, 0, 0.05); border: 1px solid #e2e8f0;">
          
          <!-- Header Banner -->
          <tr>
            <td style="background: linear-gradient(135deg, #064e3b 0%, #047857 100%); padding: 32px 30px; text-align: left;">
              <table width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td>
                    <div style="font-size: 20px; font-weight: 800; color: #ffffff; letter-spacing: 0.5px; text-transform: uppercase;">
                      Skelton Realty Group
                    </div>
                    <div style="font-size: 13px; color: #a7f3d0; margin-top: 4px; font-weight: 500;">
                      Residential Leasing & Property Management
                    </div>
                  </td>
                  <td align="right" valign="top">
                    <span style="display: inline-block; background-color: #ecfdf5; color: #065f46; font-size: 11px; font-weight: 700; padding: 6px 12px; border-radius: 9999px; text-transform: uppercase; letter-spacing: 0.5px;">
                      Payment Due
                    </span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Body Content -->
          <tr>
            <td style="padding: 30px;">
              
              <!-- Greeting -->
              <div style="font-size: 16px; color: #0f172a; margin-bottom: 12px; font-weight: 600;">
                Dear {recipient_name},
              </div>
              <div style="font-size: 14px; color: #475569; line-height: 1.6; margin-bottom: 24px;">
                Your invoice has been issued by Skelton Realty Group and is now ready for settlement. Please review the summary and itemized breakdown below.
              </div>

              <!-- Invoice Highlights Card -->
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 10px; margin-bottom: 24px; padding: 18px 20px;">
                <tr>
                  <td style="vertical-align: top; padding-right: 15px;">
                    <div style="font-size: 11px; text-transform: uppercase; color: #64748b; font-weight: 700; letter-spacing: 0.5px;">Invoice Number</div>
                    <div style="font-size: 15px; font-weight: 700; color: #0f172a; margin-top: 4px; font-family: monospace;">{invoice.invoice_number}</div>
                    
                    <div style="font-size: 11px; text-transform: uppercase; color: #64748b; font-weight: 700; letter-spacing: 0.5px; margin-top: 14px;">Issue Date</div>
                    <div style="font-size: 13px; color: #334155; margin-top: 4px;">{issued_str}</div>
                  </td>
                  <td style="vertical-align: top; text-align: right;">
                    <div style="font-size: 11px; text-transform: uppercase; color: #64748b; font-weight: 700; letter-spacing: 0.5px;">Due Date</div>
                    <div style="font-size: 13px; font-weight: 600; color: #dc2626; margin-top: 4px;">{due_str}</div>

                    <div style="font-size: 11px; text-transform: uppercase; color: #64748b; font-weight: 700; letter-spacing: 0.5px; margin-top: 14px;">Total Amount Due</div>
                    <div style="font-size: 26px; font-weight: 800; color: #064e3b; margin-top: 2px;">{total_str}</div>
                  </td>
                </tr>
              </table>

              <!-- Itemized Breakdown Table -->
              <div style="font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; color: #0f172a; margin-bottom: 10px;">
                Itemized Breakdown
              </div>
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border: 1px solid #e2e8f0; border-radius: 8px; border-collapse: separate; overflow: hidden; margin-bottom: 26px;">
                <thead>
                  <tr style="background-color: #f1f5f9;">
                    <th align="left" style="padding: 10px 14px; font-size: 12px; font-weight: 700; color: #475569; text-transform: uppercase;">Description</th>
                    <th align="center" style="padding: 10px 14px; font-size: 12px; font-weight: 700; color: #475569; text-transform: uppercase; width: 60px;">Qty</th>
                    <th align="right" style="padding: 10px 14px; font-size: 12px; font-weight: 700; color: #475569; text-transform: uppercase; width: 110px;">Amount</th>
                  </tr>
                </thead>
                <tbody>
                  {items_html}
                  <tr style="background-color: #f8fafc;">
                    <td colspan="2" style="padding: 14px; font-size: 14px; font-weight: 700; color: #0f172a; text-align: right;">Total Due:</td>
                    <td style="padding: 14px; font-size: 16px; font-weight: 800; color: #064e3b; text-align: right;">{total_str}</td>
                  </tr>
                </tbody>
              </table>

              <!-- Primary Call To Action Button -->
              <div style="text-align: center; margin: 30px 0;">
                <table width="100%" cellpadding="0" cellspacing="0" border="0">
                  <tr>
                    <td align="center">
                      <a href="{payment_cta_url}" target="_blank" style="background-color: #059669; color: #ffffff; display: inline-block; padding: 15px 36px; font-size: 15px; font-weight: 700; text-decoration: none; border-radius: 8px; box-shadow: 0 4px 6px -1px rgba(5, 150, 105, 0.3); letter-spacing: 0.3px;">
                        Pay Online via Resident Portal
                      </a>
                    </td>
                  </tr>
                </table>
                <div style="font-size: 12px; color: #64748b; margin-top: 10px;">
                  Access your resident account to complete payment and view payment rails.
                </div>
              </div>

              <!-- Payment Memo Callout Box -->
              <div style="background-color: #fffbeb; border: 1px solid #fde68a; border-left: 4px solid #f59e0b; border-radius: 6px; padding: 14px 16px; margin-bottom: 24px;">
                <div style="font-size: 13px; font-weight: 700; color: #92400e; margin-bottom: 4px;">
                  Important Payment Memo Requirement
                </div>
                <div style="font-size: 13px; color: #78350f; line-height: 1.5;">
                  Please include Invoice Number <strong style="font-family: monospace; color: #92400e;">{invoice.invoice_number}</strong> in the memo, description, or reference field when submitting your payment.
                </div>
              </div>

              <!-- Manual Payment Rails Section -->
              <div style="font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; color: #0f172a; margin-bottom: 12px;">
                Authorized Direct Payment Rails
              </div>
              <div style="margin-bottom: 24px;">
                {payment_rails_html}
              </div>

              <!-- Proof of Payment Upload Notice -->
              <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 14px 16px; margin-bottom: 24px;">
                <div style="font-size: 13px; font-weight: 700; color: #0f172a; margin-bottom: 4px;">
                  Submitting Confirmation
                </div>
                <div style="font-size: 13px; color: #475569; line-height: 1.5;">
                  Once your transfer is complete, please log in to the Resident Portal at <a href="{payment_cta_url}" style="color: #059669; text-decoration: underline; font-weight: 600;">Resident Portal Payments</a> to submit your payment reference or upload your transaction screenshot for rapid accounting verification.
                </div>
              </div>

            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background-color: #f8fafc; border-top: 1px solid #e2e8f0; padding: 24px 30px; text-align: center; font-size: 12px; color: #64748b; line-height: 1.6;">
              <div style="font-weight: 700; color: #334155; margin-bottom: 4px;">
                Skelton Realty Group Accounts & Billing
              </div>
              <div>213 Bob Ln, Virginia Beach, VA 23454</div>
              <div>Phone: (800) 555-0198 &bull; Email: accounting@skeltonrealtygroup.com</div>
              <div style="margin-top: 12px; font-size: 11px; color: #94a3b8;">
                This automated invoice notification was sent regarding your lease account. If you believe you received this in error, please contact our leasing department.
              </div>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""


def send_invoice_email(invoice: Invoice) -> bool:
    """
    Dispatches a formal invoice notification to the resident or applicant,
    complete with rich branded HTML layout, itemized breakdown, CTA link to pay,
    and manual payment details.
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

    # Format line items for plain-text fallback
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

    # Build active manual payment methods for plain-text fallback
    active_methods = PaymentMethodConfig.objects.filter(is_active=True).order_by("display_name")
    manual_sections = []

    for method in active_methods:
        kind_name = method.get_method_display()
        header = f"[{kind_name}]" if not method.display_name or method.display_name == kind_name else f"[{kind_name} - {method.display_name}]"
        lines = [header]
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

    body_text = f"""Dear {recipient_name},

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

    html_content = _build_invoice_html(invoice, recipient_name, payment_cta_url)
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "info@skeltonrealtygroup.com")

    try:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=body_text,
            from_email=from_email,
            to=[recipient_email],
        )
        msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=False)
        return True
    except Exception:
        # Fall back to queue_email with rendered HTML
        try:
            from apps.integrations.models import OutboundEmail, deliver_now
            outbound = OutboundEmail.objects.create(
                to_email=recipient_email,
                subject=subject,
                body_text=body_text,
                body_html=html_content,
                template="invoice",
            )
            deliver_now(outbound)
            return True
        except Exception:
            return False

