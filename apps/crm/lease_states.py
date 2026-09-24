"""
What each state where we have homes requires of a residential lease.

Researched September 2026 against the statutes (citations inline). Every rule
here is a floor the lease must clear; the lease's own terms are chosen to clear
all of them at once where possible - 48 hours' notice before entry satisfies
every state's minimum, a 14-day deposit return is inside every deadline, and
the late fee (the lesser of $50 or 5%) is under every cap. Where one state is
stricter than the uniform term, the rule below raises it for that state only.

THIS IS NOT LEGAL ADVICE AND IS NOT A SUBSTITUTE FOR COUNSEL. It records what
the statutes said when checked, so the lease can apply it consistently; an
attorney licensed in each state should review it, and the review notes live in
documents/lease-legal-review.md. Local ordinances (Chicago, Minneapolis,
Seattle, the California cities with rent control, and others) add rules this
table does not model.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StateRule:
    #: The earliest a late fee may be charged, in days after the due date.
    min_late_days: int = 0
    #: Minimum hours' notice before non-emergency entry.
    entry_hours: int = 0
    #: Latest a deposit (or the itemised deductions) must be returned.
    deposit_return_days: int = 30
    #: The deposit must be held in a named bank/escrow account the tenant is told about.
    deposit_bank_required: bool = False
    #: A signed move-in condition checklist is required.
    checklist_required: bool = False
    #: Owner flood-zone knowledge must be disclosed ("yes"/"no").
    flood_zone_required: bool = False
    #: Owner's knowledge of past flooding must be disclosed.
    flood_history_required: bool = False
    #: Other owner-known hazards (radon, mold, inspection orders) must be answered.
    other_hazards_required: bool = False
    #: Addendum sections for this state: (heading, [(text, strong)]).
    addendum: tuple = field(default_factory=tuple)
    #: Statutes relied on, for the attorney review.
    sources: tuple = field(default_factory=tuple)


def _p(text, strong=False):
    return (text, strong)


EARLY_TERMINATION = _p(
    "Tenants may have special statutory rights to terminate the lease early in certain situations "
    "involving family violence, certain sexual offenses or stalking, or a military deployment or transfer.",
)

STATE_RULES: dict[str, StateRule] = {
    "AZ": StateRule(
        entry_hours=48, deposit_return_days=14, checklist_required=True,
        addendum=(
            ("Arizona notices", (
                _p("The Arizona Residential Landlord and Tenant Act is available on the Arizona Department of Housing's website (housing.az.gov)."),
                _p("You will receive a move-in form to record any existing damage to the home. You may be present at the move-out inspection; tell us in writing if you would like to be."),
                _p("Any fee in this lease not described as non-refundable is refundable."),
            )),
        ),
        sources=("A.R.S. 33-1321 (deposit cap 1.5 months, move-in form, 14 business days)", "A.R.S. 33-1322 (owner/manager disclosure, Act availability)", "A.R.S. 33-1343 (2 days' notice)"),
    ),
    "CA": StateRule(
        entry_hours=24, deposit_return_days=21, flood_zone_required=True, other_hazards_required=True,
        addendum=(
            ("California rent and eviction protections", (
                _p(
                    "California law limits the amount your rent can be increased. See Section 1947.12 of the Civil "
                    "Code for more information. California law also provides that after all of the tenants have "
                    "continuously and lawfully occupied the property for 12 months or more or at least one of the "
                    "tenants has continuously and lawfully occupied the property for 24 months or more, a landlord "
                    "must provide a statement of cause in any notice to terminate a tenancy. See Section 1946.2 of "
                    "the Civil Code for more information.",
                    True,
                ),
            )),
            ("Registered sex offender database", (
                _p(
                    "Notice: Pursuant to Section 290.46 of the Penal Code, information about specified registered sex "
                    "offenders is made available to the public via an Internet Web site maintained by the Department "
                    "of Justice at www.meganslaw.ca.gov. Depending on an offender's criminal history, this information "
                    "will include either the address at which the offender resides or the community of residence and "
                    "ZIP Code in which the offender resides.",
                ),
            )),
            ("Information about bed bugs", (
                _p("Bed bugs are small, flat, reddish-brown insects about 1/4 inch long as adults that feed on blood, usually at night. They hide in mattress seams, bed frames, furniture, cracks and behind loose wallpaper. An average bed bug lives for about 10 months, and females lay one to five eggs a day, so a small problem grows quickly."),
                _p("Signs include small red to reddish-brown fecal spots on mattresses, box springs, bed frames, linens, upholstery or walls; molted skins; tiny white eggs; a sweet musty odor; and itchy bites, often in a line."),
                _p("Bed bugs are not a sign of poor housekeeping, and they are much easier to treat when caught early. Please do not treat them yourself: tell us promptly and cooperate with inspection and treatment, including preparing the home as the pest professional asks."),
                _p("How to report: tell us in writing - through your resident portal's maintenance request, or by email to the managing agent - as soon as you suspect bed bugs. We will arrange an inspection by a licensed pest professional."),
            )),
            ("Flood hazard disclosure", (
                _p("{flood_zone_sentence}"),
                _p("You may obtain information about hazards, including flood hazards, that may affect the property from the Internet Web site of the Office of Emergency Services (myhazards.caloes.ca.gov)."),
                _p("The owner's insurance does not cover the loss of the tenant's personal possessions, and it is recommended that you consider purchasing renter's insurance and flood insurance to insure your possessions from loss due to fire, flood, or other risk of loss."),
                _p("The owner is not required to provide additional information concerning the flood hazards to the property, and the information provided pursuant to this section is deemed adequate to inform the tenant."),
            )),
        ),
        sources=("Civ. Code 1950.5 (deposit cap one month from 1 July 2024; 21 days)", "Civ. Code 1946.2, 1947.12 (tenant protection notice, 12-point)", "Civ. Code 2079.10a (Megan's Law notice)", "Civ. Code 1954.603 (bed bugs)", "Gov. Code 8589.45 (flood)", "Civ. Code 1954 (24 hours' written notice)"),
    ),
    "CO": StateRule(
        min_late_days=7, deposit_return_days=30,
        addendum=(
            ("Colorado notices", (
                _p("No late fee is charged until rent is at least seven calendar days late, and a late fee is only charged if we give you written notice of it within 180 days of the missed due date. You will never be evicted, or have your tenancy ended, for failing to pay a late fee."),
                _p("A pet deposit, if any, will not exceed $300 and is refundable. Pet rent, if any, will not exceed the greater of $35 a month or 1.5% of the monthly rent."),
                _p("The home is covered by Colorado's warranty of habitability (C.R.S. 38-12-503). We will not keep any part of your deposit for normal wear and tear or for damage that existed before you moved in, and any deduction will be explained in writing with supporting documentation."),
            )),
        ),
        sources=("C.R.S. 38-12-105 (late fee: 7 days, greater of $50/5%, 180-day notice)", "HB23-1068 (pet deposit $300, pet rent cap)", "C.R.S. 38-12-103 as amended by HB25-1249 (deposit return, wear and tear)"),
    ),
    "FL": StateRule(
        entry_hours=24, deposit_return_days=15, deposit_bank_required=True,
        addendum=(
            ("Florida security deposit notice", (
                _p("Your deposit is held at: {deposit_held_at}."),
                _p(
                    "YOUR LEASE REQUIRES PAYMENT OF CERTAIN DEPOSITS. THE LANDLORD MAY TRANSFER ADVANCE RENTS TO THE "
                    "LANDLORD'S ACCOUNT AS THEY ARE DUE AND WITHOUT NOTICE. WHEN YOU MOVE OUT, YOU MUST GIVE THE LANDLORD "
                    "YOUR NEW ADDRESS SO THAT THE LANDLORD CAN SEND YOU NOTICES REGARDING YOUR DEPOSIT. THE LANDLORD MUST "
                    "MAIL YOU NOTICE, WITHIN 30 DAYS AFTER YOU MOVE OUT, OF THE LANDLORD'S INTENT TO IMPOSE A CLAIM "
                    "AGAINST THE DEPOSIT. IF YOU DO NOT REPLY TO THE LANDLORD STATING YOUR OBJECTION TO THE CLAIM WITHIN "
                    "15 DAYS AFTER RECEIPT OF THE LANDLORD'S NOTICE, THE LANDLORD WILL COLLECT THE CLAIM AND MUST MAIL YOU "
                    "THE REMAINING DEPOSIT, IF ANY. IF THE LANDLORD FAILS TO TIMELY MAIL YOU NOTICE, THE LANDLORD MUST "
                    "RETURN THE DEPOSIT BUT MAY LATER FILE A LAWSUIT AGAINST YOU FOR DAMAGES. IF YOU FAIL TO TIMELY OBJECT "
                    "TO A CLAIM, THE LANDLORD MAY COLLECT FROM THE DEPOSIT, BUT YOU MAY LATER FILE A LAWSUIT CLAIMING A "
                    "REFUND. YOU SHOULD ATTEMPT TO INFORMALLY RESOLVE ANY DISPUTE BEFORE FILING A LAWSUIT. GENERALLY, THE "
                    "PARTY IN WHOSE FAVOR A JUDGMENT IS RENDERED WILL BE AWARDED COSTS AND ATTORNEY FEES PAYABLE BY THE "
                    "LOSING PARTY. THIS DISCLOSURE IS BASIC. PLEASE REFER TO PART II OF CHAPTER 83, FLORIDA STATUTES, TO "
                    "DETERMINE YOUR LEGAL RIGHTS AND OBLIGATIONS.",
                    True,
                ),
            )),
            ("Radon gas", (
                _p(
                    "RADON GAS: Radon is a naturally occurring radioactive gas that, when it has accumulated in a building "
                    "in sufficient quantities, may present health risks to persons who are exposed to it over time. "
                    "Levels of radon that exceed federal and state guidelines have been found in buildings in Florida. "
                    "Additional information regarding radon and radon testing may be obtained from your county health department.",
                    True,
                ),
            )),
        ),
        sources=("Fla. Stat. 83.49(2)-(3) (deposit notice, 15/30 days)", "Fla. Stat. 404.056(5) (radon)", "Fla. Stat. 83.53 (24 hours, 7:30am-8pm)", "Fla. Stat. 83.50 (landlord name/address)"),
    ),
    "GA": StateRule(
        deposit_return_days=30, deposit_bank_required=True, checklist_required=True, flood_history_required=True,
        addendum=(
            ("Georgia notices", (
                _p("Your deposit is held in an escrow account used only for security deposits, at: {deposit_held_at}."),
                _p("Before we accept your deposit you will receive a list of any existing damage to the home. You have the right to inspect the home to check the list is accurate and to add anything we missed; you will not be charged for damage recorded on it."),
                _p("Flooding: {flood_history}. (Georgia law requires disclosure where flooding has damaged the living space at least three times in the five years before this lease.)"),
                _p("The home will be kept fit for human habitation throughout the tenancy. Before filing to end the tenancy for unpaid rent, we will give you at least three business days' written notice to pay."),
            )),
        ),
        sources=("O.C.G.A. 44-7-3 (owner/agent disclosure)", "O.C.G.A. 44-7-20 (flooding 3 times in 5 years)", "O.C.G.A. 44-7-31, -33, -34 (escrow, move-in list, 30 days)", "HB 404 Safe at Home Act 2024 (2-month cap, habitability, 3-day notice)"),
    ),
    "IL": StateRule(
        deposit_return_days=30, other_hazards_required=True,
        addendum=(
            ("Illinois notices", (
                _p("Radon: radon is a naturally occurring radioactive gas that can build up in homes and is a leading cause of lung cancer. The Illinois Emergency Management Agency recommends that all homes below the third floor be tested (iemaohs.illinois.gov/nrs/radon). Records or reports the owner has about radon or other hazards in the home: {other_hazards_known}. If you test for radon, please share the results with us within 10 days."),
                _p("Nothing in this lease limits the Landlord's liability for injury or damage caused by the negligence of the Landlord or its agents."),
            )),
        ),
        sources=("420 ILCS 46/25 (radon disclosure)", "765 ILCS 705/1 (exculpatory clauses void)", "765 ILCS 710 (30/45 days)"),
    ),
    "MN": StateRule(
        entry_hours=24, deposit_return_days=21, other_hazards_required=True,
        addendum=(
            ("Minnesota notices", (
                _p("Your deposit earns simple interest of one percent a year, paid to you when it is returned (Minn. Stat. 504B.178)."),
                _p("The Minnesota Attorney General publishes \"Landlords and Tenants: Rights and Responsibilities\", which explains your rights. It is available at www.ag.state.mn.us, and we will give you a copy on request."),
                _p("Outstanding inspection or condemnation orders, and other conditions the owner must disclose: {other_hazards_known}."),
                _p("We will enter only between 8:00 a.m. and 8:00 p.m., except in an emergency or when you agree otherwise."),
            )),
        ),
        sources=("Minn. Stat. 504B.178 (1% interest, 3 weeks)", "504B.181 (owner/manager, AG handbook)", "504B.195 (inspection orders)", "504B.211 (24 hours, 8am-8pm)", "504B.177 (late fee 8% cap)"),
    ),
    "NC": StateRule(
        min_late_days=5, deposit_return_days=30, deposit_bank_required=True,
        addendum=(
            ("North Carolina notices", (
                _p("Your deposit is held in a trust account (or covered by a bond) at: {deposit_held_at}."),
                _p("A late fee may be charged only once for each late payment, and only when rent is five or more calendar days late."),
            )),
        ),
        sources=("N.C.G.S. 42-46 (late fee: 5 days, greater of $15/5%, once)", "42-50 (trust account/bond, notice)", "42-51, 42-52 (2-month cap, 30 days)"),
    ),
    "NV": StateRule(
        min_late_days=3, entry_hours=24, deposit_return_days=30, checklist_required=True,
        addendum=(
            ("Nevada notices", (
                _p("A late fee is never more than 5 percent of the monthly rent and is not increased because of an earlier late fee."),
                _p("A signed record of the inventory and condition of the home will be completed with you at move-in."),
                _p("Nuisance: under NRS 202.470, a person who maintains or permits a nuisance on property is guilty of a misdemeanor. Nuisances and building, health or safety code violations can be reported to the local code enforcement office or health authority for the city or county where the home is, as well as to us."),
                _p("You have the right under Nevada law (chapter 118A of NRS) to display the flag of the United States, and to display religious or cultural items on your entry door or door frame, within the limits that law sets."),
            )),
        ),
        sources=("NRS 118A.200 (required lease contents)", "118A.210 (late fee: 3 days, 5% cap)", "118A.242 (3-month cap, 30 days)", "118A.330 (24 hours)"),
    ),
    "SC": StateRule(
        entry_hours=24, deposit_return_days=30,
        addendum=(),
        sources=("S.C. Code 27-40-410 (30 days)", "27-40-420 (owner disclosure)", "27-40-530 (24 hours)"),
    ),
    "TN": StateRule(
        min_late_days=5, deposit_return_days=30, deposit_bank_required=True,
        addendum=(
            ("Tennessee notices", (
                _p("Your deposit is held in a separate account at: {deposit_held_at}."),
                _p("A late fee is never more than ten percent of the rent that is past due, and is not charged during the five-day grace period that includes the due date."),
            )),
        ),
        sources=("T.C.A. 66-28-201 (late fee 10%, 5-day grace)", "66-28-301 (separate account, disclose bank)"),
    ),
    "TX": StateRule(
        min_late_days=3, deposit_return_days=30, flood_zone_required=True, flood_history_required=True,
        addendum=(
            ("Texas notices", (
                EARLY_TERMINATION,
                _p(
                    "Repairs: if we fail to repair a condition that materially affects the physical health or safety of an "
                    "ordinary tenant after you have given notice as the law requires and you are current on rent, you may "
                    "have remedies under Sections 92.056 and 92.0561 of the Texas Property Code, including ending this "
                    "lease, having the condition repaired or remedied and deducting the cost from your rent, and obtaining "
                    "a court order, rent reduction, damages and attorney's fees.",
                    True,
                ),
                _p("24-hour emergency repairs: {emergency_phone}."),
                _p("The home's door locks will be rekeyed before you move in, and it will have the security devices Texas law requires."),
            )),
            ("Texas flood disclosure notice (separate notice, given before you sign)", (
                _p("{flood_zone_sentence} (A \"100-year floodplain\" is an area with a 1% chance of flooding in any year.)"),
                _p("{flood_history}"),
                _p("Even when a home is not in a 100-year floodplain, it may still be at risk of flooding. Flood maps are available at msc.fema.gov. Most tenant insurance policies do not cover damage or loss incurred in a flood; you should consider buying flood insurance."),
            )),
        ),
        sources=("Tex. Prop. Code 92.016(f), 92.017(g) (early termination statement)", "92.056(g) (repair remedies, bold)", "92.0135 (flood notice, separate document)", "92.019 (late fee: 2 full days, 12%/10% cap)", "92.020 (24-hour emergency number)", "92.103 (30 days)", "92.153 (rekey)"),
    ),
    "UT": StateRule(
        entry_hours=24, deposit_return_days=30,
        addendum=(
            ("Utah notices", (
                _p("Any non-refundable part of a deposit or fee is identified as non-refundable in this lease. Anything not so identified is refundable."),
            )),
        ),
        sources=("Utah Code 57-17-2 (non-refundable in writing)", "57-17-3 (30 days)", "57-22-4 (24 hours unless lease says otherwise)"),
    ),
    "WA": StateRule(
        min_late_days=5, entry_hours=48, deposit_return_days=30, deposit_bank_required=True, checklist_required=True,
        addendum=(
            ("Washington notices", (
                _p("Your deposit is held in a trust account at: {deposit_held_at}. You will receive a written receipt for it."),
                _p("A written checklist of the condition and cleanliness of the home, signed and dated by both of us, will be completed at the start of the tenancy, and you will receive a copy. No deposit is collected without it."),
                _p("No late fee is charged until rent is more than five days past due. If your main income is a regular monthly government payment that arrives after the rent due date, you may ask in writing to move your due date by up to five days, and we will agree."),
                _p("Fire safety: the home has working smoke alarms (and a carbon monoxide alarm where required). Test them monthly, replace batteries as needed, and tell us at once if one does not work. Know two ways out of every room. Information on fire safety is available from the Washington State Patrol Fire Protection Bureau."),
                _p("Mold: moisture can lead to mold, which can affect health. Keep the home ventilated, use exhaust fans, and report leaks and damp at once. The Washington State Department of Health publishes information about mold at doh.wa.gov."),
                _p("Rent increases require at least 90 days' written notice and are limited by RCW 59.18.140 and the rent stabilization provisions of chapter 59.18 RCW. The tenancy may be ended by the landlord only for a cause listed in RCW 59.18.650."),
            )),
        ),
        sources=("RCW 59.18.260, .270 (checklist, trust account, depository notice)", "59.18.060(12),(14),(16) (fire safety, mold, landlord name)", "59.18.170 (late fee after 5 days, due-date change)", "59.18.150 (2 days' notice)", "59.18.140, .650 (90-day increase notice, just cause)", "59.18.280 (30 days)"),
    ),
}


def rule_for(state: str) -> StateRule:
    return STATE_RULES.get((state or "").upper(), StateRule())
