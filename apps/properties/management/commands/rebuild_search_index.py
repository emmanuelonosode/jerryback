from django.core.management.base import BaseCommand

from apps.properties.models import Property


class Command(BaseCommand):
    """
    Repopulate `search_text` for every property.

    Needed after the column is added, and after any bulk import that writes
    rows without going through `save()` - `bulk_create` and `update()` both
    bypass it, so a feed sync can leave rows invisible to search while they
    are perfectly visible to the filters.
    """

    help = "Rebuild the denormalised search haystack on every property."

    def handle(self, *args, **options):
        updated = 0
        batch = []
        for home in Property.objects.all().iterator(chunk_size=500):
            text = home.build_search_text()
            if text != home.search_text:
                home.search_text = text
                batch.append(home)
            if len(batch) >= 500:
                Property.objects.bulk_update(batch, ["search_text"])
                updated += len(batch)
                batch = []
        if batch:
            Property.objects.bulk_update(batch, ["search_text"])
            updated += len(batch)
        self.stdout.write(self.style.SUCCESS(f"Rebuilt search text on {updated} propertie(s)."))
