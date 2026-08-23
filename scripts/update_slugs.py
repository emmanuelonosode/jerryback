import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from apps.properties.models import Property

updated = 0
for p in Property.objects.all():
    parts = p.slug.split('-')
    changed = False
    
    # Remove trailing digits if they are not the zip code
    if parts[-1].isdigit() and parts[-1] != p.zip_code:
        parts.pop()
        changed = True
        
    new_slug = "-".join(parts)
    if not new_slug.startswith("srg-"):
        new_slug = f"srg-{new_slug}"
        changed = True
        
    if changed:
        p.slug = new_slug
        # Bypass custom save() so we don't accidentally trample anything else
        Property.objects.filter(pk=p.pk).update(slug=new_slug)
        updated += 1

print(f"Updated {updated} properties.")
