"""
Lead scoring, 0-100.

A PURE FUNCTION OF THE LEAD PLUS THE CURRENT TIME, NOT A STORED COLUMN. The
formula reads activity count, age in days, and status, so a stored value is
wrong the moment a day passes.

WHAT THE SCORE IS AND IS NOT. It ranks who to call first. It is NOT a
qualification decision and must never gate one — screening runs against the
published two-tier criteria applied consistently, which is the Fair Housing safe
harbour this business depends on. A behavioural score deciding who may apply
would be undocumented discretion wearing a number, which is precisely the
exposure the published criteria exist to remove.

Consequently the inputs are confined to intent and engagement: how they arrived,
how reachable they are, how soon they need to move. Nothing touches income,
credit, household composition, or any proxy for a protected class. Household
size and pets score for being ANSWERED, not for their value, so a family of five
scores exactly like a single occupant.
"""

from django.utils import timezone

PAID_SOURCES = {"google", "facebook", "instagram"}


def score_lead(lead, now=None) -> dict:
    now = now or timezone.now()
    reasons: list[dict] = []

    def add(label: str, points: int) -> None:
        if points:
            reasons.append({"label": label, "points": points})

    if lead.source == "PROPERTY_INQUIRY":
        add("Asked about a specific home", 25)
    if lead.phone:
        add("Gave a phone number", 15)
    if lead.property_interest_id:
        add("Named a property", 15)
    if lead.budget_min_cents is not None or lead.budget_max_cents is not None:
        add("Stated a budget", 10)
    if lead.utm_source and lead.utm_source.lower() in PAID_SOURCES:
        add("Arrived from a paid channel", 10)

    if lead.move_in_timeline == "ASAP":
        add("Needs to move now", 15)
    elif lead.move_in_timeline in ("1_3_MONTHS", "3_6_MONTHS"):
        add("Has a move-in window", 8)

    # Scored for being answered, not for the answer.
    if lead.occupants_count is not None:
        add("Told us the household size", 5)
    if lead.has_pets is not None:
        add("Answered the pets question", 3)
    if lead.preferred_contact:
        add("Said how to reach them", 4)

    activities = lead.activities.count() if lead.pk else 0
    if activities:
        add(f"{activities} recorded interaction(s)", min(activities * 5, 20))

    age_days = (now - lead.created_at).days if lead.created_at else 0
    if age_days > 30 and lead.status == "NEW":
        add("Untouched for over 30 days", -15)

    score = sum(r["points"] for r in reasons)

    if lead.status == "LOST":
        penalty = min(30, score)
        if penalty:
            add("Marked lost", -penalty)
        score = max(score - 30, 0)
    elif lead.status in ("CONVERTED", "NEGOTIATING"):
        before = score
        score = min(score + 20, 100)
        add(f"Marked {lead.status.lower()}", score - before)

    return {"score": min(max(score, 0), 100), "reasons": reasons}


def score_band(score: int) -> str:
    if score >= 75:
        return "hot"
    if score >= 50:
        return "warm"
    if score >= 25:
        return "cool"
    return "cold"
