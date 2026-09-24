"""
Take plaintext SSNs and licence numbers out of `draft_data`, and clear the
placeholder landlord that used to be every application's default.

PII. The draft JSON held the full SSN and licence number in plaintext, readable
by the anonymous draft endpoint and present in every backup. Each value is
moved into its encrypted column (only when that column is still empty - a
column value was written by the same save and is at least as current), the last
four digits are kept for identification, and the keys are deleted.

LANDLORD. `landlord_*` defaulted to one named person, a placeholder street and
a reserved 555 number. Those defaults are cleared wherever they were never
changed and no lease has been signed; a signed lease is a record of what was
agreed and is left exactly as it is.
"""

from django.db import migrations

SENSITIVE = ("ssn", "driversLicense", "ein")

OLD_DEFAULTS = {
    "landlord_name": "Kenneth Hensley Jr",
    "landlord_company": "Skelton Realty Group",
    "landlord_address": "213 Bob Ln, Virginia Beach, VA 23454",
    "landlord_email": "kenneth@skeltonrealtygroup.com",
    "landlord_phone": "(800) 555-0198",
}


def forwards(apps, schema_editor):
    RentalApplication = apps.get_model("crm", "RentalApplication")

    for app in RentalApplication.objects.all().iterator():
        fields = []
        data = dict(app.draft_data or {})

        if any(k in data for k in SENSITIVE):
            ssn = str(data.get("ssn") or "")
            digits = "".join(c for c in ssn if c.isdigit())
            if digits and not app.ssn:
                app.ssn = ssn[:255]
                fields.append("ssn")
            if len(digits) >= 4 and not app.ssn_last4:
                app.ssn_last4 = digits[-4:]
                fields.append("ssn_last4")
            licence = str(data.get("driversLicense") or "")
            if licence and not app.drivers_license_number:
                app.drivers_license_number = licence[:255]
                fields.append("drivers_license_number")
            ein = str(data.get("ein") or "")
            if ein and not app.ein:
                app.ein = ein[:10]
                fields.append("ein")
            app.draft_data = {k: v for k, v in data.items() if k not in SENSITIVE}
            fields.append("draft_data")

        if app.lease_signed_at is None:
            for field, old in OLD_DEFAULTS.items():
                if getattr(app, field) == old:
                    setattr(app, field, "")
                    fields.append(field)

        if fields:
            app.save(update_fields=fields)


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0008_guarantor_document_requests_lease_audit"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
