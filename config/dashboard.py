"""
The admin landing page.

WHY THIS REPLACES THE DEFAULT APP LIST.

Django's index lists every model grouped by the app that happens to contain it.
That grouping is an artefact of how the code is organised, not how anyone works,
and this project already has a sidebar grouped by job — so the default index
duplicates every entry and contradicts it, listing "Crm" and "Portal" next to a
sidebar that deliberately never says those words.

What replaces it answers the only question worth asking on arrival: is anything
late? Every number below is a promise with a person on the other end of it —
a decision inside 24 hours, money someone has already sent, a document blocking
a viewing, a repair in someone's home. A queue nobody can see is a queue nobody
works.
"""

from django.urls import reverse
from django.utils import timezone


def _card(title, value, url, tone="default", description=""):
    return {"title": title, "value": value, "url": url, "tone": tone, "description": description}


def callback(request, context):
    from apps.billing.models import Invoice, InvoiceStatus, Payment, PaymentStatus
    from apps.crm.models import Lead, LeadStatus, RentalApplication
    from apps.portal.models import MaintenanceRequest, MaintenanceStatus
    from apps.properties.models import Property, RENTABLE_STATUSES
    from apps.scheduler.models import TourRequest, TourStatus

    now = timezone.now()

    # --- Promises that may already be broken -------------------------------
    overdue = RentalApplication.objects.filter(
        decision_due_at__isnull=False, decided_at__isnull=True, decision_due_at__lt=now,
    ).count()
    due_soon = RentalApplication.objects.filter(
        decision_due_at__gte=now, decision_due_at__lte=now + timezone.timedelta(hours=6),
        decided_at__isnull=True,
    ).count()
    awaiting_payment = Payment.objects.filter(status=PaymentStatus.PENDING_VERIFICATION).count()
    tours_to_review = TourRequest.objects.filter(status=TourStatus.PENDING_REVIEW).count()
    urgent_maintenance = MaintenanceRequest.objects.filter(
        priority="URGENT",
    ).exclude(status__in=[MaintenanceStatus.RESOLVED, MaintenanceStatus.CLOSED]).count()

    context["kpi"] = [
        _card(
            "Decisions overdue", overdue,
            reverse("admin:crm_rentalapplication_changelist"),
            "danger" if overdue else "default",
            "Past the 24 hours promised publicly",
        ),
        _card(
            "Decisions due within 6h", due_soon,
            reverse("admin:crm_rentalapplication_changelist"),
            "warning" if due_soon else "default",
            "Clock started at payment verification",
        ),
        _card(
            "Payments to verify", awaiting_payment,
            reverse("admin:billing_payment_changelist"),
            "warning" if awaiting_payment else "default",
            "Money already sent, waiting on a person",
        ),
        _card(
            "Tour IDs to review", tours_to_review,
            reverse("admin:scheduler_tourrequest_changelist"),
            "warning" if tours_to_review else "default",
            "Blocking someone from seeing a home",
        ),
    ]

    # --- Inventory truthfulness ---------------------------------------------
    # Manual entry means drift is a staffing problem, not a technical one. These
    # two numbers are how it stays visible.
    stale_cutoff = now - timezone.timedelta(days=14)
    published = Property.objects.filter(is_published=True)
    stale = published.filter(last_verified_at__lt=stale_cutoff).count()
    never_verified = published.filter(last_verified_at__isnull=True).count()
    no_photo = published.exclude(images__is_primary=True).distinct().count()

    context["inventory"] = [
        _card("Live homes", published.filter(status__in=RENTABLE_STATUSES).count(),
              reverse("admin:properties_property_changelist"), "success",
              "Published and rentable"),
        _card("Unverified over 14 days", stale,
              reverse("admin:properties_property_changelist"),
              "danger" if stale else "default",
              "Nobody has confirmed these against reality"),
        _card("Never verified", never_verified,
              reverse("admin:properties_property_changelist"),
              "warning" if never_verified else "default",
              "Published without a check"),
        _card("Published with no photo", no_photo,
              reverse("admin:properties_property_changelist"),
              "danger" if no_photo else "default",
              "Reads as a scam listing in this category"),
    ]

    # --- Traffic --------------------------------------------------------------
    # Folded from the spool by `manage.py process_telemetry`, so these lag the
    # live site by however often that runs. Stated on the cards rather than
    # implied, because a visitor count that is quietly an hour old is worse
    # than one that says so.
    from apps.analytics.models import PageVisit, RawTelemetryEvent, Visitor, VisitorSession

    day_ago = now - timezone.timedelta(hours=24)
    visitors_total = Visitor.objects.count()
    sessions_today = VisitorSession.objects.filter(start_time__gte=day_ago).count()
    views_today = PageVisit.objects.filter(entry_time__gte=day_ago).count()
    unprocessed = RawTelemetryEvent.objects.filter(processed=False).count()

    context["traffic"] = [
        _card("Visitors", visitors_total,
              reverse("admin:analytics_visitor_changelist"), "default",
              "Distinct people seen, all time"),
        _card("Sessions today", sessions_today,
              reverse("admin:analytics_visitorsession_changelist"), "default",
              "Visits started in the last 24 hours"),
        _card("Page views today", views_today,
              reverse("admin:analytics_pagevisit_changelist"), "default",
              "Pages opened in the last 24 hours"),
        _card("Waiting to be folded in", unprocessed,
              reverse("admin:analytics_rawtelemetryevent_changelist"),
              "warning" if unprocessed > 500 else "default",
              "Run process_telemetry to bring the numbers current"),
    ]

    # --- Pipeline ------------------------------------------------------------
    context["pipeline"] = [
        {"label": label, "count": Lead.objects.filter(status=value).count(),
         "url": f"{reverse('admin:crm_lead_changelist')}?status__exact={value}"}
        for value, label in LeadStatus.choices
    ]

    # --- Money ---------------------------------------------------------------
    outstanding = Invoice.objects.filter(status=InvoiceStatus.SENT)
    outstanding_cents = sum(i.total_cents - i.received_cents for i in outstanding)
    context["money"] = {
        "outstanding_count": outstanding.count(),
        "outstanding_cents": outstanding_cents,
        # Converted here rather than in the template: cents are the storage
        # unit and the template should not be doing money arithmetic.
        "outstanding_dollars": outstanding_cents // 100,
        "overdue_count": outstanding.filter(due_date__lt=timezone.localdate()).count(),
        "url": reverse("admin:billing_invoice_changelist"),
    }

    # --- Configuration this service cannot invent ----------------------------
    # Mirrors the public site's launch gate: these are business decisions, and a
    # blank one is visible here rather than discovered by a renter.
    from apps.billing.models import PaymentMethodConfig

    blockers = []
    if not PaymentMethodConfig.objects.filter(is_active=True).exists():
        blockers.append("No payment method is live — no application can be completed.")
    if not published.exists():
        blockers.append("No published inventory.")
    if not Property.objects.filter(voucher_accepted=True).exists() and published.exists():
        blockers.append("No home is marked as accepting vouchers, which every page promises.")
    context["blockers"] = blockers

    return context
