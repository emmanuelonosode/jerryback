"""
What happens to feed data before it becomes a Property.

Two transformations, both applied on every ingest and both idempotent, so a
record that arrives unchanged produces a byte-identical result and the sync can
skip writing it.

WHY THIS IS ITS OWN MODULE. Both rules are business decisions with consequences
- one is what we may legally say, the other is what we charge - and burying
them inside a 200-line management command makes them invisible and untestable.
Here they are pure functions over strings and integers, exercised by
`tests.py`, and the command reads as "fetch, clean, compare, write".
"""

from __future__ import annotations

import re

from apps.core.money import Cents, basis_points_of

# ---------------------------------------------------------------------------
# Branding
# ---------------------------------------------------------------------------

BRAND = "Skelton Realty Group"

"""
Names and domains belonging to the companies whose feeds we ingest.

THE PROBLEM THIS SOLVES. Descriptions arrive written by the managing partner
and say so: "Invitation Homes is pleased to present...", "contact Invitation
Homes to schedule". Published unedited on our own listing page that tells a
renter a different company manages the home, and in several cases links them to
it. It is also the single most common way a competitor's brand ends up indexed
against our URLs.

Ordered longest-first so "Invitation Homes" is replaced before "Invitation"
could be, and matched case-insensitively on word boundaries so "invitational"
is left alone.
"""
_BRAND_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Full URLs first, and SUBSTITUTED rather than deleted. Removing them left
    # the sentence around them dangling - "Contact us at for details" - so the
    # link becomes ours instead, which is both grammatical and correct.
    (re.compile(r"https?://(?:www\.)?invitationhomes\.com[^\s<>\"']*", re.I), "skeltonrealtygroup.com"),
    (re.compile(r"https?://(?:www\.)?primefamilyhousing\.com[^\s<>\"']*", re.I), "skeltonrealtygroup.com"),
    (re.compile(r"\b(?:www\.)?invitationhomes\.com\b", re.I), "skeltonrealtygroup.com"),
    (re.compile(r"\b(?:www\.)?primefamilyhousing\.com\b", re.I), "skeltonrealtygroup.com"),
    # Then the brand names themselves.
    (re.compile(r"\bInvitation\s+Homes(?:,?\s+(?:LLC|Inc\.?))?\b", re.I), BRAND),
    (re.compile(r"\bPrime\s+Family\s+Housing(?:,?\s+(?:LLC|Inc\.?))?\b", re.I), BRAND),
    (re.compile(r"\bIH\s*Merger\s*Sub[^.,;]*", re.I), BRAND),
]

# Markup arrives in the feed's description field. It is stripped rather than
# rendered: the frontend prints these as text, and passing a partner's HTML
# through would be an injection hole with extra steps.
_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"[ \t]{2,}")
_BLANK_LINES = re.compile(r"\n{3,}")


def clean_description(raw: str | None) -> str:
    """
    Strip a partner's markup and replace their brand with ours.

    NOT A REWRITE. Sentences that mention nobody pass through untouched - what
    a renter reads is still the description written for that home. Only the
    brand references change, and only to the name of the company actually
    letting the property.
    """
    if not raw:
        return ""

    text = _TAG.sub(" ", str(raw))
    for pattern, replacement in _BRAND_PATTERNS:
        text = pattern.sub(replacement, text)

    # Tidy the seams the substitutions leave: a removed URL takes its
    # surrounding spaces with it, and stripped tags leave runs of them.
    text = _WHITESPACE.sub(" ", text)
    text = re.sub(r" +([,.;:!?])", r"\1", text)
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip()


def clean_text(raw: str | None) -> str:
    """Brand substitution for short fields - titles, neighbourhoods, captions."""
    if not raw:
        return ""
    text = str(raw)
    for pattern, replacement in _BRAND_PATTERNS:
        text = pattern.sub(replacement, text)
    return _WHITESPACE.sub(" ", text).strip()


def mentions_foreign_brand(text: str | None) -> bool:
    """Whether anything in `_BRAND_PATTERNS` still appears. For tests and audits."""
    if not text:
        return False
    return any(pattern.search(str(text)) for pattern, _ in _BRAND_PATTERNS)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

"""
The discount off the partner's advertised rent.

BASE RENT ONLY. Fees are passed through untouched, and `total_monthly_cents`
is computed as rent plus required monthly fees - so discounting here flows
into every displayed total automatically and cannot accidentally discount a
security deposit or a pet fee.

BASIS POINTS, NOT A FLOAT. 2000 bp is exactly 20%; `0.2` is not exactly 0.2,
and a rent of $1,795 multiplied by it lands a cent either side depending on
the platform.

IDEMPOTENT BY CONSTRUCTION. The discount is always taken off the ORIGINAL
price, never off the current one, so running the sync twice - or re-basing a
record that was already re-based - produces the same number rather than
compounding to 36%.
"""
DISCOUNT_BASIS_POINTS = 2000


def discounted_rent(original_cents: Cents | None) -> Cents:
    """The advertised base rent: the partner's price less `DISCOUNT_BASIS_POINTS`."""
    base = int(original_cents or 0)
    if base <= 0:
        return 0
    return base - basis_points_of(base, DISCOUNT_BASIS_POINTS)
