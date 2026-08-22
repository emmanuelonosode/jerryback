import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from apps.properties.models import Property

for p in Property.objects.all()[:10]:
    parts = p.slug.split('-')
    if parts[-1].isdigit() and parts[-1] != p.zip_code:
        parts.pop()
    new_slug = "-".join(parts)
    if not new_slug.startswith("srg-"):
        new_slug = f"srg-{new_slug}"
    print(f"{p.slug} -> {new_slug}")
