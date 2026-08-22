"""
Branded HTML for outbound email.

WHY THE COMPANY FACTS ARE REPEATED HERE. The site reads them from `.env` at
build time through `lib/content/business.ts`; Django cannot see that file, and
duplicating the values as literals would guarantee the two drift apart. So the
same environment variables are read here, by the same names, and the licence
list - which is structured content rather than a scalar - is mirrored from
`lib/content/licensing.ts` with a single source comment on each side.
"""

import re

from django.conf import settings
from django.template.loader import render_to_string
from django.utils.html import strip_tags

# Mirrors lib/content/licensing.ts. Keep the two in step: a licence number that
# is wrong in an email is wrong in exactly the place it will be relied on.
LICENCES = [
    ("AL", "99815"), ("AZ", "BR036908000"), ("AR", "PB00094183"), ("CA", "01265072"),
    ("CO", "ER.100004013"), ("CT", "REB0793977"), ("DE", "RB-0031267"),
    ("DC", "BR200201382"), ("FL", "BK3612122"), ("GA", "419264"), ("HI", "RB-21790"),
    ("IL", "471018764"), ("IN", "RB21000257"), ("IA", "B70527000"), ("KY", "179364"),
    ("LA", "BROK.77122-ACT"), ("MD", "5009570"), ("MI", "6502431855"),
    ("MN", "40234608"), ("MS", "24027"), ("MO", "2021037321"), ("NE", "20240035"),
    ("NV", "B.1002762.LLC"), ("NH", "80310"), ("NJ", "1433509"), ("NM", "20857"),
]


def _env(name: str, default: str = "") -> str:
    import os

    return (os.environ.get(name) or default).strip()


def _phones():
    raw = _env("NEXT_PUBLIC_COMPANY_PHONE")
    out = []
    for part in (p.strip() for p in raw.split("|")):
        if not part:
            continue
        digits = re.sub(r"\D", "", part)
        display = part
        if len(digits) == 10 and not re.search(r"[^\s\d]", part):
            display = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
        out.append({"display": display, "tel": digits or part})
    return out


def email_context(subject: str, body_text: str) -> dict:
    site_url = settings.PUBLIC_SITE_URL.rstrip("/")
    address = [line.strip() for line in _env("NEXT_PUBLIC_COMPANY_ADDRESS").split("|") if line.strip()]
    return {
        "subject": subject,
        "body_text": body_text,
        # The first line of the body, trimmed - what a client shows next to the
        # subject before anyone opens it.
        "preheader": " ".join(strip_tags(body_text).split())[:140],
        "site_url": site_url,
        "site_host": re.sub(r"^https?://", "", site_url).rstrip("/"),
        "company_name": _env("NEXT_PUBLIC_COMPANY_LEGAL_NAME", "Skelton Realty Group"),
        "address_lines": address,
        "phones": _phones(),
        "email": _env("NEXT_PUBLIC_COMPANY_EMAIL"),
        "licences": [{"state": s, "number": n} for s, n in LICENCES],
    }


def render_email_html(subject: str, body_text: str) -> str:
    """Wrap a plain-text body in the branded header and footer."""
    return render_to_string("email/base.html", email_context(subject, body_text))
