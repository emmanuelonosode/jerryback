"""
Ready-made messages staff can send from the admin.

WHY A CATALOGUE AND NOT A FREE TEXTAREA. Every one of these gets sent while
somebody is waiting on a decision about where they are going to live, and the
things that must be in them - the reference to quote, what happens next, when -
are exactly the things that get left out when you are typing quickly. The
templates carry that structure; the person sending still edits every word
before it goes.

PLACEHOLDERS are plain `{name}` fields. They are filled in by hand in the
compose form, not resolved automatically: an address auto-inserted from the
wrong record is worse than a blank someone has to fill.

The branded header and footer are NOT here. They are applied to every message
by `branding.render_email_html`, so a template cannot accidentally ship without
the logo, the licences, or the anti-fraud wording.
"""

MESSAGE_TEMPLATES = {
    "blank": {
        "label": "Blank message",
        "subject": "",
        "body": "",
    },
    "application-received": {
        "label": "Application received",
        "subject": "We have your application for {property}",
        "body": (
            "Hi {first_name},\n\n"
            "Your application for {property} is with us and an agent is reading it.\n\n"
            "Your reference is {reference}. Quote it if you contact us about this.\n\n"
            "What happens next: once your application fee is confirmed, you get a "
            "decision within 24 hours, and we tell you the reason either way.\n\n"
            "If anything about your circumstances would help us understand the "
            "application, reply to this email and tell us. It will not count "
            "against you."
        ),
    },
    "payment-confirmed": {
        "label": "Payment confirmed",
        "subject": "We have received your payment",
        "body": (
            "Hi {first_name},\n\n"
            "Your payment of {amount} has been confirmed against reference {reference}.\n\n"
            "Your 24-hour decision window starts now, from this confirmation rather "
            "than from when you sent the money.\n\n"
            "Nothing further is due until a lease is signed."
        ),
    },
    "documents-needed": {
        "label": "Something we need from you",
        "subject": "One thing we need for your application",
        "body": (
            "Hi {first_name},\n\n"
            "We are working through your application for {property} and we need "
            "one more thing:\n\n"
            "  {what_we_need}\n\n"
            "You can reply to this email with it attached, and quote {reference}.\n\n"
            "This is not a problem with your application - we just cannot finish "
            "reading it without this."
        ),
    },
    "tour-confirmed": {
        "label": "Tour confirmed",
        "subject": "Your tour of {property} is confirmed",
        "body": (
            "Hi {first_name},\n\n"
            "You are booked to see {property} on {when}.\n\n"
            "Bring photo ID. There is nothing to pay at the tour, and nobody will "
            "ask you for a deposit or a holding fee while you are there.\n\n"
            "If you need to move it, reply to this email."
        ),
    },
    "move-in-next-steps": {
        "label": "Approved — move-in next steps",
        "subject": "Next steps for {property}",
        "body": (
            "Hi {first_name},\n\n"
            "Here is what happens between now and getting your keys for {property}.\n\n"
            "1. Move-in costs. The breakdown is on your portal, and you can pay it "
            "there.\n"
            "2. Lease signing. We book this once the payment clears.\n"
            "3. Keys. Handed over at signing.\n\n"
            "Quote {reference} on anything you send us."
        ),
    },
    "general-update": {
        "label": "General update",
        "subject": "An update on your application",
        "body": (
            "Hi {first_name},\n\n"
            "{message}\n\n"
            "Your reference is {reference} if you need to contact us about this."
        ),
    },
}

#: Ordered for the picker, blank first so the default is an empty message.
TEMPLATE_CHOICES = [(key, value["label"]) for key, value in MESSAGE_TEMPLATES.items()]
