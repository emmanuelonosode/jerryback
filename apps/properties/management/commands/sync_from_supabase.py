"""
Daily refresh of the catalogue from the Supabase data lake.

WHAT THIS REPLACED, AND WHY IT MATTERED.

The previous version wrote every field of every property on every run and then
DELETED and recreated all of its images, amenities and fees - whether or not
anything had changed. On 4,482 properties that is ~78,000 image rows destroyed
and reinserted nightly, every `updated_at` bumped, and every primary key
rotated. Three consequences, all bad:

  * `updated_at` became the time of the last sync rather than the time the home
    last changed, so `lastmod` in the sitemap told Google that all 4,482 pages
    changed every night. That is how a sitemap stops being believed.
  * Editors could not see what actually moved, because everything moved.
  * It was an enormous amount of write traffic on a 1-vCPU box shared with the
    web server.

So this compares first and writes only what differs. A night where the feed is
unchanged now performs zero writes and says so.

IT ALSO CLOSES A DATA-FRESHNESS HOLE. The old command fetched only
`status=eq.available` and never looked at anything else, so a home that left
the feed - leased, withdrawn - stayed `available` on our site indefinitely.
Anything previously synced and now absent is retired at the end of the run.
"""

from __future__ import annotations

import os
import re
import time

import requests
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from apps.properties.ingest import clean_description, clean_text, discounted_rent
from apps.properties.models import (
    Property,
    PropertyAmenity,
    PropertyFee,
    PropertyImage,
)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://okrlwuoqnwujffzyzazw.supabase.co")
SUPABASE_KEY = os.environ.get(
    "SUPABASE_KEY", "sb_publishable_zlmVAZvMIGGFYuM5Xd13cw_JGRt-2QH"
)

PAGE = 100

# Feed fee rows that merely restate the rent. Counting them would double the
# advertised total; the model has the same guard for the same reason.
RENT_RESTATEMENTS = {"base rent", "base monthly rent", "rent", "monthly rent"}


def _natural_key(address: str, zip_code: str) -> str:
    """
    The identity of a home, independent of whatever slug it was first given.

    THE SLUG IS NOT AN IDENTITY. Two importers gave the same houses two
    different slugs - an older run prefixed them `srg-`, the current feed does
    not - and matching on slug therefore treated 3,093 already-listed homes as
    brand new while retiring the records Google had indexed. Every one of those
    4,476 indexed URLs went `noindex` in a single run, and 4,643 URLs Google
    had never seen replaced them.

    Address plus ZIP is what actually identifies a house. Normalised hard,
    because the same address arrives as "1465 Lake Lucerne Rd SW" and
    "1465 lake lucerne rd sw" from different passes.
    """
    normalised = re.sub(r"[^a-z0-9]+", " ", f"{address} {zip_code}".lower()).strip()
    return re.sub(r"\s+", " ", normalised)


def _date(value):
    """
    Feed date to a `date`, because that is what the column returns.

    NOT COSMETIC. `available_from` is a DateField, so `getattr()` hands back a
    `datetime.date` while the feed sends `"2026-09-13"`. Comparing the two is
    always unequal, which reported every dated property as changed on every
    run - 4,641 of 4,643 of them - and defeated the whole point of comparing.
    """
    from datetime import date, datetime

    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _int(value):
    """Feed number to `int` or None. Same class of problem as `_date`."""
    if value in (None, ""):
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _cents(value) -> int:
    try:
        return int(round(float(str(value).replace(",", "").replace("$", "")) * 100))
    except (TypeError, ValueError):
        return 0


def _image_urls(raw) -> list[str]:
    """The feed ships images as strings, dicts, or reprs of dicts."""
    import ast

    if isinstance(raw, str):
        raw = [raw]
    urls: list[str] = []
    for item in raw or []:
        if isinstance(item, dict):
            url = item.get("image_url")
        elif isinstance(item, str) and item.startswith("{") and "'image_url':" in item:
            try:
                url = ast.literal_eval(item).get("image_url")
            except (ValueError, SyntaxError):
                url = item
        else:
            url = item
        if url:
            urls.append(str(url))
    return urls


class Command(BaseCommand):
    help = "Sync available properties from Supabase, writing only what changed."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything.",
        )
        parser.add_argument(
            "--no-retire",
            action="store_true",
            help="Skip retiring properties that have left the feed.",
        )

    # -- fetching -----------------------------------------------------------

    def _fetch_all(self) -> list[dict]:
        headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
        rows: list[dict] = []
        offset = 0
        while True:
            url = (
                f"{SUPABASE_URL}/rest/v1/properties"
                f"?status=eq.available&select=*&limit={PAGE}&offset={offset}"
            )
            for attempt in range(3):
                try:
                    response = requests.get(url, headers=headers, timeout=30)
                    break
                except requests.exceptions.RequestException as error:
                    self.stderr.write(f"  network error at offset {offset}: {error}")
                    time.sleep(2 * (attempt + 1))
            else:
                # PARTIAL DATA IS WORSE THAN NO DATA HERE. Returning what we
                # have would look like "the feed shrank", and the retire pass
                # at the end would then mark every missing home as leased.
                raise RuntimeError(f"Supabase unreachable at offset {offset}")

            if response.status_code != 200:
                raise RuntimeError(f"Supabase returned {response.status_code}: {response.text[:300]}")

            batch = response.json()
            if not batch:
                break
            rows.extend(batch)
            offset += PAGE
        return rows

    # -- mapping ------------------------------------------------------------

    def _defaults_for(self, p: dict, agent_id) -> dict:
        """
        The feed row as our columns.

        `original_price_cents` is the partner's advertised rent, kept verbatim
        so the discount can always be recomputed from source rather than
        compounded off an already-discounted number. `price_cents` is what we
        advertise. Fees are untouched - the discount is base rent only.
        """
        original = _cents(p.get("price"))
        bathrooms = p.get("bathrooms") or 0
        try:
            half_baths = int(float(bathrooms) * 2)
        except (TypeError, ValueError):
            half_baths = 0

        address = clean_text(p.get("address"))
        city = clean_text(p.get("city"))

        return {
            "title": clean_text(p.get("title")),
            "address": address,
            "city": city,
            "state": (p.get("state") or "")[:2],
            "zip_code": p.get("zip_code") or "",
            "original_price_cents": original,
            "price_cents": discounted_rent(original),
            "bedrooms": _int(p.get("bedrooms")) or 0,
            "half_bathrooms": half_baths,
            "sqft": _int(p.get("sqft")) or 0,
            "description": clean_description(p.get("description")),
            "year_built": _int(p.get("year_built")),
            "latitude": p.get("latitude"),
            "longitude": p.get("longitude"),
            "tour_3d_url": p.get("virtual_tour_url") or "",
            "schools": p.get("schools") or [],
            "raw_fees": p.get("fees") or [],
            "office_info": p.get("office") or {},
            "floor_plans": p.get("floor_plans") or [],
            "available_from": _date(p.get("available_on")),
            "pets_allowed": bool(p.get("is_pet_friendly")),
            "listing_type": p.get("listing_type") or "for-rent",
            "lot_size": _int(p.get("lot_size")) or 0,
            "condition": p.get("condition") or "",
            "cross_street": p.get("cross_street") or "",
            "tour_360_url": p.get("tour_360_url") or "",
            "has_pool": bool(p.get("has_pool")),
            "allow_selfshow": bool(p.get("allow_selfshow")),
            "source_url": p.get("source_url") or "",
            "api_endpoint": p.get("api_endpoint") or "",
            "raw_data": p.get("raw_data") or {},
            "status": "available",
            "type": "residential",
            "price_label": "/mo",
            "garage": 0,
            "stories": 1,
            "neighborhood": clean_text(p.get("market_name")),
            "is_published": True,
            "agent_id": agent_id,
            # `search_text` is DELIBERATELY ABSENT.
            #
            # `Property.save()` rebuilds it from the address, city, state and
            # market on every write - lowercased, punctuation stripped, region
            # appended - so any value set here is overwritten immediately and
            # then differs from the stored one for ever after. That single
            # field made the change detector report all 4,643 properties as
            # changed on every run, which is precisely the nightly write storm
            # this rewrite exists to stop.
        }

    def _fees_for(self, p: dict) -> list[dict]:
        out = []
        for i, fee in enumerate(p.get("fees") or []):
            title = clean_text(fee.get("title") or fee.get("name") or "Fee")
            if title.lower() in RENT_RESTATEMENTS:
                continue
            required = fee.get("is_required", True)
            out.append({
                "fee_key": (slugify(title)[:50] or f"fee-{i}"),
                "label": title[:120],
                "amount_cents": _cents(fee.get("fee_amount")),
                "cadence": "monthly" if str(fee.get("frequency", "")).upper() == "MONTHLY" else "one-time",
                "condition": "required" if required else "conditional",
                "reason": clean_text(fee.get("description")),
                "applies_when": "" if required else "When applicable",
                "sort_order": i,
            })
        return out

    # -- writing ------------------------------------------------------------

    def _sync_children(self, prop, model, incoming: list[dict], dry: bool) -> bool:
        """
        Reconcile a property's child rows, writing only on a real difference.

        Compares the incoming set against what is stored, on the incoming
        fields only - stored rows also carry ids and timestamps, which differ
        every time by construction and would make every comparison report a
        change. Returns whether anything was (or would be) written.

        This is what turns a nightly delete-and-reinsert of ~78,000 image rows
        into zero writes on a night when the feed has not moved.
        """
        if not incoming:
            if not model.objects.filter(property=prop).exists():
                return False
            if not dry:
                model.objects.filter(property=prop).delete()
            return True

        fields = list(incoming[0].keys())
        model_fields = {f.name for f in model._meta.get_fields()}
        ordered = "sort_order" in model_fields

        stored = list(
            model.objects
            .filter(property=prop)
            .order_by(*(["sort_order", "id"] if ordered else ["id"]))
            .values(*fields)
        )

        # ORDER MATTERS ONLY WHERE THE MODEL STORES IT.
        #
        # Images and fees carry `sort_order`, so their sequence is data and is
        # compared as such. `PropertyAmenity` does not - its primary key is a
        # UUID, so the stored order is arbitrary and will never match the feed's.
        # Comparing those as ordered lists reported all 4,619 remaining
        # properties as changed every night while their amenities were in fact
        # identical.
        if ordered:
            if stored == incoming:
                return False
        else:
            key = lambda row: tuple(sorted(row.items()))  # noqa: E731
            if sorted(stored, key=key) == sorted(incoming, key=key):
                return False

        if not dry:
            model.objects.filter(property=prop).delete()
            model.objects.bulk_create(
                [model(property=prop, **row) for row in incoming],
                batch_size=500,
            )
        return True

    def handle(self, *args, **options):
        dry = options["dry_run"]
        started = timezone.now()

        from django.contrib.auth import get_user_model

        agent, _ = get_user_model().objects.get_or_create(
            email="admin@skeltonrealtygroup.com",
            defaults={"is_staff": True, "is_superuser": True},
        )

        self.stdout.write("Fetching from Supabase…")
        rows = self._fetch_all()
        self.stdout.write(f"  {len(rows)} available properties in the feed")

        created = updated = unchanged = failed = 0
        seen_ids: set = set()

        # One pass over what we already hold, so the loop below can ask "is this
        # house already listed" without a query per feed row.
        existing_by_key = {}
        for row in Property.objects.values("id", "address", "zip_code", "slug"):
            existing_by_key.setdefault(
                _natural_key(row["address"] or "", row["zip_code"] or ""), row["id"]
            )


        for p in rows:
            slug = p.get("slug")
            if not slug:
                continue


            defaults = self._defaults_for(p, agent.id)
            try:
                with transaction.atomic():
                    # By slug first - the fast path once a home is settled -
                    # then by what the home actually is. An existing record
                    # KEEPS ITS OWN SLUG: it is the URL in the sitemap, in
                    # Google's index, and in whatever links point at it.
                    prop = Property.objects.filter(slug=slug).first()
                    if prop is None:
                        match_id = existing_by_key.get(
                            _natural_key(defaults["address"], defaults["zip_code"])
                        )
                        prop = Property.objects.filter(pk=match_id).first() if match_id else None
                    is_new = prop is None

                    if is_new:
                        if dry:
                            created += 1
                            continue
                        prop = Property.objects.create(slug=slug, **defaults)
                        field_changed = True
                    else:
                        # FIELD BY FIELD, so `updated_at` moves only when a
                        # value actually moved. This is what keeps `lastmod` in
                        # the sitemap meaningful.
                        field_changed = any(
                            getattr(prop, field, None) != value
                            for field, value in defaults.items()
                        )
                        if field_changed and not dry:
                            for field, value in defaults.items():
                                setattr(prop, field, value)
                            prop.save()

                    images = [
                        {"url": u, "source_url": u, "is_primary": i == 0, "sort_order": i}
                        for i, u in enumerate(_image_urls(p.get("images")))
                    ]
                    amenities = [
                        {"name": n, "slug": slugify(n)[:120]}
                        for n in (
                            clean_text(a.get("name") if isinstance(a, dict) else a)
                            for a in (p.get("amenities") or [])
                        )
                        if n
                    ]

                    children_changed = False
                    for model, incoming in (
                        (PropertyImage, images),
                        (PropertyAmenity, amenities),
                        (PropertyFee, self._fees_for(p)),
                    ):
                        if self._sync_children(prop, model, incoming, dry):
                            children_changed = True

                    seen_ids.add(prop.pk)

                    if is_new:
                        created += 1
                    elif field_changed or children_changed:
                        updated += 1
                    else:
                        unchanged += 1

                    # Recorded on every run, changed or not: it is the answer to
                    # "when did a person last confirm this against the source",
                    # and it deliberately does NOT touch `updated_at`.
                    if not dry:
                        Property.objects.filter(pk=prop.pk).update(last_verified_at=started)
            except Exception as error:  # noqa: BLE001 - one bad row must not stop the run
                failed += 1
                self.stderr.write(f"  {slug}: {error}")

        retired = 0
        if not options["no_retire"] and seen_ids:
            stale = Property.objects.filter(status="available").exclude(pk__in=seen_ids)
            retired = stale.count()
            if retired and not dry:
                # Retired, not deleted: the page stays reachable for its grace
                # window so an inbound link lands somewhere useful.
                stale.update(status="leased", leased_at=started, updated_at=started)

        verb = "Would sync" if dry else "Synced"
        self.stdout.write(self.style.SUCCESS(
            f"{verb}: {created} new, {updated} changed, {unchanged} unchanged, "
            f"{retired} retired, {failed} failed "
            f"({(timezone.now() - started).total_seconds():.0f}s)"
        ))
