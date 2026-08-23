import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from apps.crm.models import RentalApplication

app = RentalApplication.objects.create(
    first_name="John",
    last_name="Doe",
    ssn="123-45-6789",
    mothers_maiden_name="Smith",
    application_fee_cents=5000,
)

app_fresh = RentalApplication.objects.get(id=app.id)

print(f"Original SSN: 123-45-6789")
print(f"Retrieved SSN: {app_fresh.ssn}")
print(f"Original Maiden Name: Smith")
print(f"Retrieved Maiden Name: {app_fresh.mothers_maiden_name}")

from django.db import connection
with connection.cursor() as cursor:
    cursor.execute("SELECT ssn, mothers_maiden_name FROM crm_rentalapplication WHERE id = %s", [app.id])
    row = cursor.fetchone()
    print(f"Raw DB SSN (Encrypted): {row[0]}")
    print(f"Raw DB Maiden Name (Encrypted): {row[1]}")

app.delete()
