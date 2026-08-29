import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from apps.properties.models import Property, PropertyFee
from django.utils.text import slugify

count = 0
for prop in Property.objects.all():
    fees_list = prop.raw_fees or []
    if not fees_list:
        continue
    
    PropertyFee.objects.filter(property=prop).delete()
    for i, fee in enumerate(fees_list):
        fee_title = fee.get("title") or fee.get("name") or "Fee"
        if fee_title.lower() in ("base rent", "base monthly rent", "rent", "monthly rent"):
            continue

        fee_amount_str = str(fee.get("fee_amount") or "0").replace(",", "")
        try:
            fee_amount_cents = int(float(fee_amount_str) * 100)
        except ValueError:
            fee_amount_cents = 0

        cadence = "monthly" if fee.get("frequency", "").upper() == "MONTHLY" else "one-time"
        is_required = fee.get("is_required", True)

        PropertyFee.objects.create(
            property=prop,
            fee_key=slugify(fee_title)[:50] or f"fee-{i}",
            label=fee_title[:120],
            amount_cents=fee_amount_cents,
            cadence=cadence,
            condition="required" if is_required else "conditional",
            reason=fee.get("description") or "",
            applies_when="" if is_required else "When applicable",
            sort_order=i
        )
    count += 1

print(f"Updated fees for {count} properties.")
