"""
Money.

INTEGER CENTS EVERYWHERE — in the database, in the API, in every calculation.

Python's Decimal is exact, so the usual "floats break money" argument does not
apply here the way it does in JavaScript. This is about having ONE
representation. The public site's money model is integer cents end to end; if
this service stored Decimal and emitted "3345.00", the frontend would parse it
back to cents on receipt, and that parsing boundary is precisely where money
bugs live. Cents in, cents out, formatted only where a human reads it.

Percentages are basis points for the same reason: 3.5% of rent produces
fractional cents on most rents, and a rate stored as 0.035 invites someone to
multiply a float by a cent count.
"""

from decimal import Decimal, ROUND_HALF_UP

Cents = int


def dollars(amount: float | int | str) -> Cents:
    """Dollars to cents, rounded half-up through Decimal so 8.115 -> 812."""
    return int((Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_amount_to_cents(value: str | int | float | None) -> Cents | None:
    """
    Parse a decimal money string from an external feed into exact cents.

    Returns None rather than 0 for anything unparseable. A fee that silently
    becomes zero understates a published total, which is the one number this
    product cannot get wrong — the caller has to decide what to do about it.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value * 100
    raw = str(value).strip().replace("$", "").replace(",", "").replace(" ", "")
    if not raw:
        return None
    try:
        return int((Decimal(raw) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except Exception:
        return None


def basis_points_of(base: Cents, bp: int) -> Cents:
    """bp of base, rounded to the nearest cent. 10000 bp = 100%."""
    return int((Decimal(base) * Decimal(bp) / Decimal(10000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_usd(cents: Cents) -> str:
    whole, part = divmod(abs(int(cents)), 100)
    sign = "-" if cents < 0 else ""
    return f"{sign}${whole:,}" if part == 0 else f"{sign}${whole:,}.{part:02d}"
