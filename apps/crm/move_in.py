"""
Move-in cost calculation.

INTEGER CENTS, and defaults supplied by policy rather than invented here.

The spec defaults the security deposit to one month's rent and the lease admin
fee to $150. Both are business decisions with legal weight — deposit maximums
are capped by statute in many states — so they come from settings, and a deposit
above a configured ceiling is REPORTED rather than silently clamped. Quietly
reducing it hides a policy conflict from the person who has to resolve it, and
clamping to a cap that may not apply in this jurisdiction is its own error.
"""

from dataclasses import dataclass, field

from django.conf import settings


@dataclass
class MoveInBreakdown:
    line_items: list[dict] = field(default_factory=list)
    total_cents: int = 0
    warnings: list[str] = field(default_factory=list)


def calculate_move_in(
    *,
    monthly_rent_cents: int,
    months_upfront: int = 1,
    security_deposit_cents: int | None = None,
    application_fee_cents: int = 0,
    lease_admin_fee_cents: int | None = None,
    pet_fee_cents: int | None = None,
    max_security_deposit_cents: int | None = None,
) -> MoveInBreakdown:
    if not isinstance(monthly_rent_cents, int) or monthly_rent_cents <= 0:
        raise ValueError("Monthly rent must be a positive whole number of cents")

    months = max(1, int(months_upfront or 1))
    deposit = security_deposit_cents if security_deposit_cents is not None else monthly_rent_cents
    admin_fee = (
        lease_admin_fee_cents if lease_admin_fee_cents is not None
        else settings.MOVE_IN_LEASE_ADMIN_FEE_CENTS
    )
    pet_fee = pet_fee_cents or 0

    warnings: list[str] = []
    if max_security_deposit_cents is not None and deposit > max_security_deposit_cents:
        warnings.append(
            "Security deposit exceeds the configured ceiling for this market: "
            f"requested {deposit}c, ceiling {max_security_deposit_cents}c."
        )

    lines = [
        {
            "description": f"Rent upfront ({months} months)" if months > 1 else "First month's rent",
            "quantity": months,
            "unit_price_cents": monthly_rent_cents,
        },
        {"description": "Security deposit", "quantity": 1, "unit_price_cents": deposit},
    ]
    # Only when not already collected. Charging to apply and again at move-in is
    # exactly the quiet double-charge this brand exists not to do.
    if application_fee_cents > 0:
        lines.append({"description": "Application fee", "quantity": 1, "unit_price_cents": application_fee_cents})
    if admin_fee > 0:
        lines.append({"description": "Lease administration fee", "quantity": 1, "unit_price_cents": admin_fee})
    if pet_fee > 0:
        lines.append({"description": "Pet fee", "quantity": 1, "unit_price_cents": pet_fee})

    total = sum(line["unit_price_cents"] * line["quantity"] for line in lines)
    return MoveInBreakdown(line_items=lines, total_cents=total, warnings=warnings)
