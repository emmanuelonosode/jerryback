"""
The words of the residential lease.

WHY THE TEXT LIVES HERE AND NOT IN THE WEB PAGE. A signature is only worth
what can be proved about what was signed. With the clauses in the frontend,
the signed snapshot held the numbers but not the wording, and a later edit to
the page silently changed every lease already signed. Building the text here
puts the exact words into `lease_terms_snapshot`, which is hashed at signing;
the page only lays it out.

THE BALANCE. Tenant-favourable wherever that costs the owner nothing it needs:
no fees that are not in the listing, a late fee well under every state cap,
48 hours' notice before entry, repairs on us, a deposit back in 14 days,
subletting by consent. Protective where it matters: rent and damage beyond
normal wear remain the tenant's, the tenancy ends through the courts and never
by lockout, mitigation limits exposure on early termination, and every clause
yields to state law rather than failing outright.
"""

from .lease_states import StateRule

LEAD_WARNING = (
    "Housing built before 1978 may contain lead-based paint. Lead from paint, paint chips, and dust can pose "
    "health hazards if not managed properly. Lead exposure is especially harmful to young children and pregnant "
    "women. Before renting pre-1978 housing, lessors must disclose the presence of known lead-based paint and/or "
    "lead-based paint hazards in the dwelling. Lessees must also receive a federally approved pamphlet on lead "
    "poisoning prevention."
)


def _para(text: str, strong: bool = False) -> dict:
    return {"text": text, "strong": strong}


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def build_sections(t: dict, rule: StateRule, fill: dict) -> list[dict]:
    """
    Every clause of the lease, in order, from the terms dict `t` and the state rule.
    `fill` supplies the owner's disclosures for the state addendum placeholders.
    """
    m = t["money"]
    ll, agent, ten, prem, hh = t["landlord"], t["agent"], t["tenant"], t["premises"], t["household"]
    state = prem["state_name"] or "the state where the home is located"
    late_after = m["late_after_days"]
    entry_hours = t["entry_notice_hours"]
    s: list[dict] = []

    def section(heading, *paras):
        s.append({"heading": heading, "paragraphs": [p for p in paras if p]})

    fees_line = ""
    if m["monthly_fees"]:
        fees_line = " It is made up of base rent of {base} plus these monthly charges, each shown on the listing: {items}.".format(
            base=m["base_rent"], items="; ".join(f"{f['label']} {f['amount']}" for f in m["monthly_fees"]),
        )

    section(
        "Parties",
        _para(
            f"This Residential Lease (the \"Lease\") is between {ll['owner'] or 'the owner of the home'} (the \"Landlord\"), "
            f"acting through its managing agent, {agent['name']} (the \"Agent\"), and {ten['name'] or 'the tenant named below'} "
            f"(the \"Tenant\"). \"We\" and \"us\" mean the Landlord and the Agent acting for it; \"you\" means the Tenant."
        ),
        _para(
            "The Agent manages the home for the Landlord: it collects rent, arranges repairs, and receives notices. "
            f"The Landlord's address for notices and legal process is {ll['address'] or 'TO CONFIRM'}"
            + (f"; the person who acts for the Landlord is {ll['signatory']}" if ll["signatory"] else "")
            + f". The Agent's address is {agent['address'] or 'TO CONFIRM'}."
        ),
        _para("The Lease takes effect on the date the last of us signs it (the \"Effective Date\")."),
    )

    occupants = hh["occupants"] or (
        f"{hh['adults']} adult(s) including the Tenant" + (f" and {hh['dependents']} child(ren) or dependent(s)" if hh["dependents"] else "")
    )
    section(
        "The home and who lives there",
        _para(
            f"We rent to you the home at {prem['address'] or 'TO CONFIRM'} (the \"Home\")"
            + (f", a {prem['type'].lower()}" if prem["type"] else "")
            + (f" with {prem['bedrooms']} bedrooms and {prem['bathrooms']} bathrooms" if prem["bedrooms"] else "")
            + (f"; parking: {prem['parking']}" if prem["parking"] else "")
            + "."
        ),
        _para(
            f"The Home is for residential use by you and the approved occupants listed in your application: {occupants}. "
            "Guests may stay up to 14 nights in a row; anyone staying longer needs our written approval, which we will "
            "not unreasonably refuse. Adding an adult occupant may require a short application."
        ),
    )

    section(
        "Term",
        _para(f"The Lease starts on {t['term']['start'] or 'TO CONFIRM'} and ends on {t['term']['end'] or 'TO CONFIRM'}."),
        _para(
            "If neither of us gives written notice to end it before then, the Lease continues month to month on the same "
            "terms. Either of us may end a month-to-month tenancy by written notice of at least 30 days, or any longer "
            "period (and only for the reasons) the law of the state where the Home is located requires. There is no "
            "automatic renewal for another fixed term."
        ),
    )

    section(
        "Rent",
        _para(f"Rent is {m['total_monthly_rent'] or 'TO CONFIRM'} a month, due on the {_ordinal(m['rent_due_day'])} day of each month.{fees_line}"),
        _para(
            "There are no other monthly charges. Pay through your resident portal or any payment method we list there. "
            "If the Lease starts on a day other than the due date, the first month's rent is prorated by the day."
        ),
        _para(
            "Rent does not increase during the fixed term. Any later increase needs written notice of at least 60 days, "
            "or any longer notice (and within any limit) that state or local law requires."
        ),
    )

    section(
        "Late payment",
        _para(
            f"If rent is not paid within {late_after} days after the due date, a one-time late fee of {m['late_fee_rule']} "
            f"({m['late_fee'] or 'TO CONFIRM'} at this rent) may be charged for that month. It is charged once per late "
            "month, never on an unpaid late fee, and never more than state law allows."
        ),
        _para(
            f"A payment returned unpaid by your bank costs {m['returned_payment_fee']} (or less if state law sets a lower limit). "
            "Partial payments are accepted and applied to rent first."
        ),
        _para("If you are having trouble paying, tell us early - we would rather agree a plan than charge a fee."),
    )

    deposit_lines = [
        _para(
            f"Security deposit: {m['security_deposit'] or 'TO CONFIRM'}. It secures your obligations under the Lease; it is "
            "not a limit on what you owe and may not be used as the last month's rent unless we agree in writing."
        ),
        _para(
            f"We will return the deposit within {m['deposit_return_days']} days after the tenancy ends and you have returned "
            "the keys, with a written, itemized statement of any deductions and supporting documentation. We will not "
            "deduct for normal wear and tear or for anything that was damaged before you moved in. Please give us a "
            "forwarding address. Interest is paid on the deposit wherever state law requires it."
        ),
    ]
    if m["lease_admin_fee"]:
        deposit_lines.append(_para(
            f"Lease administration fee: {m['lease_admin_fee']}, paid once before move-in. It covers preparing the Lease and "
            "the move-in inspection, and it is non-refundable."
        ))
    if m["pet_fee"]:
        deposit_lines.append(_para(f"Pet fee: {m['pet_fee']}, for the pets listed in your application."))
    section("Deposit and move-in costs", *deposit_lines)

    section(
        "Condition of the Home",
        _para(
            "Before or on move-in day we will give you a written move-in condition checklist, with photos. You have "
            "seven days after moving in to add anything we missed; sign and return it and keep a copy. You will not be "
            "charged for anything recorded on it."
        ),
    )

    section(
        "Utilities",
        _para(
            "You pay for electricity, in your own name from the start date. We pay for water, sewer, trash collection "
            "and any gas service to the Home. Internet, cable and phone are yours to arrange if you want them."
        ),
        _para("Please do not waste the utilities we pay for, and tell us promptly about leaks or running toilets."),
    )

    emergency = t.get("emergency_phone") or ""
    section(
        "Repairs and upkeep",
        _para(
            "We keep the Home safe, fit to live in and in good repair, as the law requires. That includes the structure "
            "and roof, plumbing, electrical, heating and air conditioning, the appliances we provide, smoke and carbon "
            "monoxide alarms, and pest control (other than an infestation caused by the household). We arrange and pay "
            "for repairs through the Agent."
        ),
        _para(
            "Report problems promptly through your resident portal"
            + (f", and emergencies (no heat, a burst pipe, a gas smell, an electrical hazard) at any hour on {emergency}" if emergency else "")
            + ". We aim to respond to emergencies within 24 hours and to other repairs within a reasonable time."
        ),
        _para(
            "You keep the Home clean and sanitary, replace light bulbs and batteries you use up, and use the Home and its "
            "fixtures with ordinary care. You pay for damage beyond normal wear and tear caused by you, your household, "
            "your guests or your animals. You may withhold or deduct rent for repairs only where and as state law allows."
        ),
    )

    section(
        "Our entry",
        _para(
            f"Except in an emergency, we will give you at least {entry_hours} hours' written notice (email or portal message "
            "is fine) before entering, and enter only between 8:00 a.m. and 8:00 p.m. on a reasonable day, to make "
            "repairs, inspect, or, in the last 30 days of the tenancy, show the Home. You may be present. We will not use "
            "entry to harass you."
        ),
    )

    section(
        "Using the Home",
        _para(
            "Use the Home as a private residence and follow the law and any homeowners' association rules we give you in "
            "writing. Do not disturb neighbors or create a nuisance. You have the right to quiet enjoyment of the Home "
            "without interference from us."
        ),
    )

    section(
        "Pets and assistance animals",
        _para("Pets are allowed only as listed in your application or later approved by us in writing."),
        _para(
            "Assistance animals (service animals and support animals for a person with a disability) are not pets. They "
            "are allowed as a reasonable accommodation, and no pet fee, pet rent, pet deposit, or breed or weight limit "
            "ever applies to them. You remain responsible for damage caused by any animal."
        ),
    )

    section(
        "Changes to the Home, and locks",
        _para(
            "Ask us in writing before painting, installing fixtures or making other alterations; we will not unreasonably "
            "refuse. Hanging pictures with ordinary nails is fine. Do not change or add locks without giving us a key, "
            "except where state law lets a survivor of domestic violence change the locks. The locks will be rekeyed "
            "before you move in."
        ),
    )

    section(
        "Subletting and assignment",
        _para(
            "You may sublet the Home or assign this Lease only with our written consent, which we will not unreasonably "
            "withhold or delay. You remain responsible under the Lease unless we release you in writing."
        ),
    )

    section(
        "Insurance and responsibility",
        _para(
            "The Landlord's insurance does not cover your belongings or your liability. We strongly recommend renter's "
            "insurance, including liability cover."
        ),
        _para(
            "Each of us is responsible for harm caused by our own negligence or intentional acts. Nothing in this Lease "
            "limits the Landlord's or the Agent's responsibility for their own negligence or where the law does not allow it."
        ),
    )

    section(
        "Fire, flood or other damage",
        _para(
            "If the Home becomes wholly or partly unlivable through no fault of the household, rent is reduced for the time "
            "and extent the Home cannot be used. If it cannot be repaired within a reasonable time, either of us may end "
            "the Lease by written notice, and prepaid rent and the deposit are returned as the Lease provides."
        ),
    )

    section(
        "Fair housing and reasonable accommodations",
        _para(
            "We do not discriminate on the basis of race, color, religion, sex (including sexual orientation and gender "
            "identity), national origin, familial status, disability, or any other characteristic protected by federal, "
            "state or local law, including source of income where protected."
        ),
        _para(
            "If you or someone in your household has a disability, you may ask for a reasonable accommodation in our rules "
            "or services, or a reasonable modification of the Home. Where a disability or the need is not obvious, we may "
            "ask for reliable information showing the disability-related need, but never for a diagnosis or medical records."
        ),
    )

    section(
        "Ending the Lease early",
        _para(
            "Tenants may have special statutory rights to terminate the lease early in certain situations involving family "
            "violence, certain sexual offenses or stalking, or a military deployment or transfer, including under the "
            "federal Servicemembers Civil Relief Act. We will honor them."
        ),
        _para(
            "Otherwise, you may end the Lease early by giving at least 30 days' written notice. You remain responsible for "
            "rent until the end date or until a new tenant starts paying rent, whichever comes first, and we will make "
            "reasonable efforts to re-rent the Home quickly. There is no early-termination penalty beyond that."
        ),
    )

    section(
        "If the Lease is broken",
        _para(
            "If rent is unpaid or another term of the Lease is broken, we will give you written notice and the time to "
            "pay or put it right that state law requires. The tenancy can be ended against your will only through the "
            "courts: we will never lock you out, remove your belongings, or shut off utilities to make you leave."
        ),
        _para(
            "If we fail to meet our obligations, you have the remedies state law gives you. In any lawsuit about this "
            "Lease, the winning party may recover reasonable attorney's fees and court costs where the law allows, and "
            "only as the law allows."
        ),
    )

    section(
        "Moving out",
        _para(
            "At the end of the tenancy, return all keys and leave the Home clean and in the condition recorded at move-in, "
            "except for normal wear and tear. You may ask us for a walk-through before you leave. Property left behind is "
            "handled as state law requires."
        ),
    )

    section(
        "Notices",
        _para(
            f"Send notices to us through your resident portal or to the Agent at {agent['email'] or 'TO CONFIRM'}"
            + (f" / {agent['phone']}" if agent["phone"] else "")
            + f", or by mail to {agent['address'] or 'TO CONFIRM'}. We send notices to you at the Home and to "
            f"{ten['email'] or 'your email address'}. Where state law requires a notice in a particular form, we will use that form."
        ),
    )

    if prem["lead_paint_disclosure"]:
        section(
            "Lead-based paint disclosure",
            _para(LEAD_WARNING, True),
            _para(
                "Owner's disclosure of known lead-based paint or lead-based paint hazards, and of any records or reports: "
                + (fill.get("lead_hazards_known") or "TO CONFIRM") + "."
            ),
            _para(
                "You will receive the EPA pamphlet \"Protect Your Family From Lead in Your Home\" with this Lease. The Agent "
                "has informed the owner of its obligations under 42 U.S.C. 4852d and is aware of its responsibility to ensure compliance."
            ),
        )

    if t.get("guarantor"):
        section(
            "Guarantor",
            _para(
                f"{t['guarantor']['name']} has offered to guarantee this Lease and will sign a separate guaranty. The guaranty "
                "covers rent and charges under this Lease only, and it ends when the Lease and any month-to-month tenancy end."
            ),
        )

    section(
        "General terms",
        _para(
            f"This Lease is governed by the law of {state}. If any part of it conflicts with that law or any local law, the "
            "law controls and that part is changed only as far as needed; the rest of the Lease still applies."
        ),
        _para(
            "This Lease, its addenda and your application are the whole agreement. Changes must be in writing and "
            "signed by both of us. Not enforcing a term once does not waive it. Electronic signatures and copies count "
            "as originals, and you may ask for a paper copy at no charge."
        ),
    )

    for heading, paras in rule.addendum:
        section(
            f"Addendum: {heading}",
            *(_para(text.format(**fill), strong) for text, strong in paras),
        )

    # Number them, so "Section 5" in a conversation means one thing.
    for i, sec in enumerate(s, start=1):
        sec["number"] = i
    return s
