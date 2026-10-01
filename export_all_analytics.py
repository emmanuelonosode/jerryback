#!/usr/bin/env python3
"""
Export all traffic, visitor, session, telemetry, application, lead, tour, and billing data
from the production Skelton Realty Group database for offline and online analytics.
"""

import os
import sys
import json
import csv
import gzip
from datetime import datetime, date
from collections import Counter, defaultdict

import django

# Setup Django environment
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.db.models import Count, Sum, Avg, Min, Max
from django.utils import timezone

from apps.analytics.models import Visitor, VisitorSession, PageVisit, TelemetryEvent, RawTelemetryEvent
from apps.crm.models import RentalApplication, Lead, Guarantor, ApplicationDocument
from apps.scheduler.models import TourRequest, Viewing
from apps.billing.models import Payment, Invoice
from apps.properties.models import Property


def json_serial(obj):
    """JSON serializer for objects not serializable by default json code"""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if hasattr(obj, "hex"):
        return str(obj)
    return str(obj)


def cents_to_dollars(cents):
    if cents is None:
        return ""
    return f"{cents / 100:.2f}"


def export_all(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    print(f"[{datetime.now().isoformat()}] Starting complete analytics data extraction into: {out_dir}")

    # =========================================================================
    # 1. RENTAL APPLICATIONS
    # =========================================================================
    print("Exporting Rental Applications...")
    apps_qs = RentalApplication.objects.select_related("property", "lead", "user", "guarantor").prefetch_related("documents").all()
    
    app_csv_path = os.path.join(out_dir, "applications.csv")
    app_json_path = os.path.join(out_dir, "applications_detailed.json")
    
    apps_data_list = []
    
    app_fieldnames = [
        "id", "status", "property_id", "property_address", "property_city", "property_state", "property_zip",
        "property_monthly_rent", "lead_id", "user_id", "user_email", "application_fee_dollars", "is_fee_paid",
        "first_name", "middle_name", "last_name", "email", "cell_phone", "date_of_birth",
        "present_address", "city", "state", "zip_code", "how_long_at_address", "reason_for_leaving",
        "current_landlord_name", "current_landlord_phone", "move_in_date", "months_rent_upfront",
        "security_deposit_dollars", "lease_admin_fee_dollars", "pet_fee_dollars",
        "id_type", "ssn_last4", "screening_reference", "ein", "marital_status", "drivers_license_state",
        "previous_address", "previous_city", "previous_state", "previous_zip", "previous_residence_months",
        "gross_monthly_income_dollars", "employer_name", "employer_address", "job_title", "supervisor_phone",
        "voucher_covers_dollars", "has_kids", "number_of_kids", "has_pets", "animals_count",
        "has_felony_eviction_bankruptcy", "is_active_military", "has_housing_assistance",
        "certification_text", "recovery_email_sent", "verified_at", "decision_due_at", "decided_at",
        "decision_reason", "submitted_at", "ip_address", "utm_source", "landlord_name", "landlord_company",
        "lease_sent_at", "lease_signed_at", "has_guarantor", "documents_count"
    ]
    
    with open(app_csv_path, "w", newline="", encoding="utf-8") as f_csv:
        writer = csv.DictWriter(f_csv, fieldnames=app_fieldnames)
        writer.writeheader()
        
        for app in apps_qs:
            has_guarantor = hasattr(app, "guarantor") and app.guarantor is not None
            documents_count = app.documents.count()
            animals_list = app.animals if isinstance(app.animals, list) else []
            
            row = {
                "id": str(app.id),
                "status": app.status,
                "property_id": str(app.property.id) if app.property else "",
                "property_address": app.property.address if app.property else "",
                "property_city": app.property.city if app.property else "",
                "property_state": app.property.state if app.property else "",
                "property_zip": app.property.zip_code if app.property else "",
                "property_monthly_rent": cents_to_dollars(app.property.price_cents) if app.property else "",
                "lead_id": str(app.lead.id) if app.lead else "",
                "user_id": str(app.user.id) if app.user else "",
                "user_email": app.user.email if app.user else "",
                "application_fee_dollars": cents_to_dollars(app.application_fee_cents),
                "is_fee_paid": app.is_fee_paid,
                "first_name": app.first_name,
                "middle_name": app.middle_name,
                "last_name": app.last_name,
                "email": app.email,
                "cell_phone": app.cell_phone,
                "date_of_birth": app.date_of_birth.isoformat() if app.date_of_birth else "",
                "present_address": app.present_address,
                "city": app.city,
                "state": app.state,
                "zip_code": app.zip_code,
                "how_long_at_address": app.how_long_at_address,
                "reason_for_leaving": app.reason_for_leaving,
                "current_landlord_name": app.current_landlord_name,
                "current_landlord_phone": app.current_landlord_phone,
                "move_in_date": app.move_in_date.isoformat() if app.move_in_date else "",
                "months_rent_upfront": app.months_rent_upfront,
                "security_deposit_dollars": cents_to_dollars(app.security_deposit_cents),
                "lease_admin_fee_dollars": cents_to_dollars(app.lease_admin_fee_cents),
                "pet_fee_dollars": cents_to_dollars(app.pet_fee_cents),
                "id_type": app.id_type,
                "ssn_last4": app.ssn_last4,
                "screening_reference": app.screening_reference,
                "ein": app.ein,
                "marital_status": app.marital_status,
                "drivers_license_state": app.drivers_license_state,
                "previous_address": app.previous_address,
                "previous_city": app.previous_city,
                "previous_state": app.previous_state,
                "previous_zip": app.previous_zip,
                "previous_residence_months": app.previous_residence_months or "",
                "gross_monthly_income_dollars": cents_to_dollars(app.gross_monthly_income_cents),
                "employer_name": app.employer_name,
                "employer_address": app.employer_address,
                "job_title": app.job_title,
                "supervisor_phone": app.supervisor_phone,
                "voucher_covers_dollars": cents_to_dollars(app.voucher_covers_cents),
                "has_kids": app.has_kids if app.has_kids is not None else "",
                "number_of_kids": app.number_of_kids or "",
                "has_pets": app.has_pets if app.has_pets is not None else "",
                "animals_count": len(animals_list),
                "has_felony_eviction_bankruptcy": app.has_felony_eviction_bankruptcy if app.has_felony_eviction_bankruptcy is not None else "",
                "is_active_military": app.is_active_military if app.is_active_military is not None else "",
                "has_housing_assistance": app.has_housing_assistance if app.has_housing_assistance is not None else "",
                "certification_text": app.certification_text,
                "recovery_email_sent": app.recovery_email_sent,
                "verified_at": app.verified_at.isoformat() if app.verified_at else "",
                "decision_due_at": app.decision_due_at.isoformat() if app.decision_due_at else "",
                "decided_at": app.decided_at.isoformat() if app.decided_at else "",
                "decision_reason": app.decision_reason,
                "submitted_at": app.submitted_at.isoformat() if app.submitted_at else "",
                "ip_address": app.ip_address or "",
                "utm_source": app.utm_source,
                "landlord_name": app.landlord_name,
                "landlord_company": app.landlord_company,
                "lease_sent_at": app.lease_sent_at.isoformat() if app.lease_sent_at else "",
                "lease_signed_at": app.lease_signed_at.isoformat() if app.lease_signed_at else "",
                "has_guarantor": has_guarantor,
                "documents_count": documents_count,
            }
            writer.writerow(row)
            
            # Full structured JSON record
            full_record = dict(row)
            full_record["animals"] = animals_list
            full_record["draft_data"] = app.draft_data
            if has_guarantor:
                g = app.guarantor
                full_record["guarantor"] = {
                    "id": str(g.id), "full_name": g.full_name, "email": g.email, "phone": g.phone,
                    "relationship": g.relationship, "monthly_income_dollars": cents_to_dollars(g.monthly_income_cents),
                    "created_at": g.created_at.isoformat() if g.created_at else ""
                }
            else:
                full_record["guarantor"] = None
                
            full_record["documents"] = [
                {
                    "id": str(d.id), "kind": d.kind, "original_name": d.original_name,
                    "stored_name": d.stored_name, "size": d.size,
                    "uploaded_at": d.created_at.isoformat() if hasattr(d, "created_at") else ""
                } for d in app.documents.all()
            ]
            apps_data_list.append(full_record)

    with open(app_json_path, "w", encoding="utf-8") as f_json:
        json.dump(apps_data_list, f_json, indent=2, default=json_serial)

    print(f"  -> Exported {len(apps_data_list)} rental applications.")

    # =========================================================================
    # 2. LEADS
    # =========================================================================
    print("Exporting Leads...")
    leads_qs = Lead.objects.select_related("property_interest", "assigned_agent").all()
    lead_csv_path = os.path.join(out_dir, "leads.csv")
    lead_json_path = os.path.join(out_dir, "leads.json")
    
    lead_fieldnames = [
        "id", "full_name", "email", "phone", "source", "status", "interest_type",
        "budget_min_dollars", "budget_max_dollars", "preferred_location",
        "property_id", "property_address", "property_city", "property_state",
        "message", "utm_source", "utm_medium", "utm_campaign", "detected_city",
        "move_in_timeline", "occupants_count", "has_pets", "has_voucher",
        "preferred_contact", "assigned_agent_email", "last_contacted_at",
        "created_at", "updated_at"
    ]
    
    leads_data_list = []
    with open(lead_csv_path, "w", newline="", encoding="utf-8") as f_csv:
        writer = csv.DictWriter(f_csv, fieldnames=lead_fieldnames)
        writer.writeheader()
        
        for lead in leads_qs:
            prop = lead.property_interest
            row = {
                "id": str(lead.id),
                "full_name": lead.full_name,
                "email": lead.email,
                "phone": lead.phone,
                "source": lead.source,
                "status": lead.status,
                "interest_type": lead.interest_type,
                "budget_min_dollars": cents_to_dollars(lead.budget_min_cents),
                "budget_max_dollars": cents_to_dollars(lead.budget_max_cents),
                "preferred_location": lead.preferred_location,
                "property_id": str(prop.id) if prop else "",
                "property_address": prop.address if prop else "",
                "property_city": prop.city if prop else "",
                "property_state": prop.state if prop else "",
                "message": lead.message,
                "utm_source": lead.utm_source,
                "utm_medium": lead.utm_medium,
                "utm_campaign": lead.utm_campaign,
                "detected_city": lead.detected_city,
                "move_in_timeline": lead.move_in_timeline,
                "occupants_count": lead.occupants_count or "",
                "has_pets": lead.has_pets if lead.has_pets is not None else "",
                "has_voucher": lead.has_voucher if lead.has_voucher is not None else "",
                "preferred_contact": lead.preferred_contact,
                "assigned_agent_email": lead.assigned_agent.email if lead.assigned_agent else "",
                "last_contacted_at": lead.last_contacted_at.isoformat() if lead.last_contacted_at else "",
                "created_at": lead.created_at.isoformat() if lead.created_at else "",
                "updated_at": lead.updated_at.isoformat() if lead.updated_at else "",
            }
            writer.writerow(row)
            leads_data_list.append(row)

    with open(lead_json_path, "w", encoding="utf-8") as f_json:
        json.dump(leads_data_list, f_json, indent=2, default=json_serial)

    print(f"  -> Exported {len(leads_data_list)} leads.")

    # =========================================================================
    # 3. TOUR REQUESTS
    # =========================================================================
    print("Exporting Tour Requests...")
    tours_qs = TourRequest.objects.select_related("property", "lead").all()
    tour_csv_path = os.path.join(out_dir, "tour_requests.csv")
    tour_json_path = os.path.join(out_dir, "tour_requests.json")
    
    tour_fieldnames = [
        "id", "public_id", "lead_id", "property_id", "property_address", "property_city", "property_state",
        "full_name", "email", "phone", "preferred_date", "preferred_time", "tour_type",
        "notes", "status", "id_submitted", "id_purged_at", "reviewed_at", "rejection_reason", "created_at"
    ]
    
    tours_data_list = []
    with open(tour_csv_path, "w", newline="", encoding="utf-8") as f_csv:
        writer = csv.DictWriter(f_csv, fieldnames=tour_fieldnames)
        writer.writeheader()
        
        for tour in tours_qs:
            has_id = bool(tour.id_front_url or tour.id_back_url)
            row = {
                "id": str(tour.id),
                "public_id": str(tour.public_id),
                "lead_id": str(tour.lead.id) if tour.lead else "",
                "property_id": str(tour.property.id) if tour.property else "",
                "property_address": tour.property.address if tour.property else "",
                "property_city": tour.property.city if tour.property else "",
                "property_state": tour.property.state if tour.property else "",
                "full_name": tour.full_name,
                "email": tour.email,
                "phone": tour.phone,
                "preferred_date": tour.preferred_date.isoformat() if tour.preferred_date else "",
                "preferred_time": tour.preferred_time,
                "tour_type": tour.tour_type,
                "notes": tour.notes,
                "status": tour.status,
                "id_submitted": has_id,
                "id_purged_at": tour.id_purged_at.isoformat() if tour.id_purged_at else "",
                "reviewed_at": tour.reviewed_at.isoformat() if tour.reviewed_at else "",
                "rejection_reason": tour.rejection_reason,
                "created_at": tour.created_at.isoformat() if tour.created_at else "",
            }
            writer.writerow(row)
            tours_data_list.append(row)

    with open(tour_json_path, "w", encoding="utf-8") as f_json:
        json.dump(tours_data_list, f_json, indent=2, default=json_serial)

    print(f"  -> Exported {len(tours_data_list)} tour requests.")

    # =========================================================================
    # 4. PAYMENTS & INVOICES
    # =========================================================================
    print("Exporting Payments...")
    payments_qs = Payment.objects.select_related("rental_application", "invoice", "verified_by").all()
    pay_csv_path = os.path.join(out_dir, "payments.csv")
    pay_json_path = os.path.join(out_dir, "payments.json")
    
    pay_fieldnames = [
        "id", "invoice_id", "rental_application_id", "applicant_name", "amount_dollars",
        "payment_method", "status", "reference_id", "verified_by_email", "verified_at",
        "rejection_reason", "paid_at", "receipt_sent", "notes", "created_at"
    ]
    
    payments_data_list = []
    with open(pay_csv_path, "w", newline="", encoding="utf-8") as f_csv:
        writer = csv.DictWriter(f_csv, fieldnames=pay_fieldnames)
        writer.writeheader()
        
        for pay in payments_qs:
            applicant_name = f"{pay.rental_application.first_name} {pay.rental_application.last_name}" if pay.rental_application else ""
            row = {
                "id": str(pay.id),
                "invoice_id": str(pay.invoice.id) if pay.invoice else "",
                "rental_application_id": str(pay.rental_application.id) if pay.rental_application else "",
                "applicant_name": applicant_name.strip(),
                "amount_dollars": cents_to_dollars(pay.amount_cents),
                "payment_method": pay.payment_method,
                "status": pay.status,
                "reference_id": pay.reference_id,
                "verified_by_email": pay.verified_by.email if pay.verified_by else "",
                "verified_at": pay.verified_at.isoformat() if pay.verified_at else "",
                "rejection_reason": pay.rejection_reason,
                "paid_at": pay.paid_at.isoformat() if pay.paid_at else "",
                "receipt_sent": pay.receipt_sent,
                "notes": pay.notes,
                "created_at": pay.created_at.isoformat() if pay.created_at else "",
            }
            writer.writerow(row)
            payments_data_list.append(row)

    with open(pay_json_path, "w", encoding="utf-8") as f_json:
        json.dump(payments_data_list, f_json, indent=2, default=json_serial)

    print(f"  -> Exported {len(payments_data_list)} payments.")

    # =========================================================================
    # 5. VISITORS
    # =========================================================================
    print("Exporting Visitors...")
    visitors_qs = Visitor.objects.select_related("user").all().iterator(chunk_size=3000)
    vis_csv_path = os.path.join(out_dir, "visitors.csv")
    
    vis_fieldnames = [
        "id", "fingerprint_id", "user_id", "user_email", "first_seen", "last_seen",
        "total_sessions_count", "total_dwell_seconds", "total_dwell_minutes",
        "primary_device", "primary_city", "is_lead"
    ]
    
    visitor_count = 0
    with open(vis_csv_path, "w", newline="", encoding="utf-8") as f_csv:
        writer = csv.DictWriter(f_csv, fieldnames=vis_fieldnames)
        writer.writeheader()
        
        for v in visitors_qs:
            visitor_count += 1
            writer.writerow({
                "id": str(v.id),
                "fingerprint_id": v.fingerprint_id,
                "user_id": str(v.user.id) if v.user else "",
                "user_email": v.user.email if v.user else "",
                "first_seen": v.first_seen.isoformat() if v.first_seen else "",
                "last_seen": v.last_seen.isoformat() if v.last_seen else "",
                "total_sessions_count": v.total_sessions_count,
                "total_dwell_seconds": v.total_dwell_seconds,
                "total_dwell_minutes": round(v.total_dwell_seconds / 60, 2),
                "primary_device": v.primary_device,
                "primary_city": v.primary_city,
                "is_lead": v.is_lead,
            })

    print(f"  -> Exported {visitor_count} visitors.")

    # =========================================================================
    # 6. VISITOR SESSIONS
    # =========================================================================
    print("Exporting Visitor Sessions...")
    sessions_qs = VisitorSession.objects.all().iterator(chunk_size=3000)
    sess_csv_path = os.path.join(out_dir, "sessions.csv")
    
    sess_fieldnames = [
        "id", "session_id", "visitor_id", "ip_address", "city", "region", "country",
        "timezone", "language", "browser", "os", "device_type", "user_agent",
        "screen_width", "screen_height", "viewport_width", "viewport_height",
        "landing_page", "referrer", "utm_source", "properties_viewed_count",
        "total_dwell_seconds", "start_time", "end_time"
    ]
    
    session_count = 0
    with open(sess_csv_path, "w", newline="", encoding="utf-8") as f_csv:
        writer = csv.DictWriter(f_csv, fieldnames=sess_fieldnames)
        writer.writeheader()
        
        for s in sessions_qs:
            session_count += 1
            writer.writerow({
                "id": str(s.id),
                "session_id": s.session_id,
                "visitor_id": str(s.visitor_id),
                "ip_address": s.ip_address,
                "city": s.city,
                "region": s.region,
                "country": s.country,
                "timezone": s.timezone,
                "language": s.language,
                "browser": s.browser,
                "os": s.os,
                "device_type": s.device_type,
                "user_agent": s.user_agent,
                "screen_width": s.screen_width or "",
                "screen_height": s.screen_height or "",
                "viewport_width": s.viewport_width or "",
                "viewport_height": s.viewport_height or "",
                "landing_page": s.landing_page,
                "referrer": s.referrer,
                "utm_source": s.utm_source,
                "properties_viewed_count": s.properties_viewed_count,
                "total_dwell_seconds": s.total_dwell_seconds,
                "start_time": s.start_time.isoformat() if s.start_time else "",
                "end_time": s.end_time.isoformat() if s.end_time else "",
            })

    print(f"  -> Exported {session_count} visitor sessions.")

    # =========================================================================
    # 7. PAGE VISITS
    # =========================================================================
    print("Exporting Page Visits...")
    pages_qs = PageVisit.objects.all().iterator(chunk_size=5000)
    pages_csv_path = os.path.join(out_dir, "page_visits.csv")
    
    page_fieldnames = [
        "id", "session_id", "path", "entry_time", "exit_time", "max_scroll_depth", "idle_seconds", "dwell_seconds"
    ]
    
    page_count = 0
    with open(pages_csv_path, "w", newline="", encoding="utf-8") as f_csv:
        writer = csv.DictWriter(f_csv, fieldnames=page_fieldnames)
        writer.writeheader()
        
        for p in pages_qs:
            page_count += 1
            dwell = ""
            if p.entry_time and p.exit_time:
                dwell = int((p.exit_time - p.entry_time).total_seconds())
                
            writer.writerow({
                "id": str(p.id),
                "session_id": str(p.session_id),
                "path": p.path,
                "entry_time": p.entry_time.isoformat() if p.entry_time else "",
                "exit_time": p.exit_time.isoformat() if p.exit_time else "",
                "max_scroll_depth": p.max_scroll_depth,
                "idle_seconds": p.idle_seconds,
                "dwell_seconds": dwell,
            })

    print(f"  -> Exported {page_count} page visits.")

    # =========================================================================
    # 8. TELEMETRY EVENTS
    # =========================================================================
    print("Exporting Telemetry Events...")
    events_qs = TelemetryEvent.objects.all().iterator(chunk_size=5000)
    events_csv_path = os.path.join(out_dir, "telemetry_events.csv")
    
    event_fieldnames = ["id", "session_id", "page_visit_id", "event_type", "event_data_json", "created_at"]
    
    event_count = 0
    with open(events_csv_path, "w", newline="", encoding="utf-8") as f_csv:
        writer = csv.DictWriter(f_csv, fieldnames=event_fieldnames)
        writer.writeheader()
        
        for e in events_qs:
            event_count += 1
            writer.writerow({
                "id": str(e.id),
                "session_id": str(e.session_id),
                "page_visit_id": str(e.page_visit_id) if e.page_visit_id else "",
                "event_type": e.event_type,
                "event_data_json": json.dumps(e.event_data) if e.event_data else "{}",
                "created_at": e.created_at.isoformat() if e.created_at else "",
            })

    print(f"  -> Exported {event_count} telemetry events.")

    # =========================================================================
    # 8b. PROPERTIES SUMMARY
    # =========================================================================
    print("Exporting Properties Summary...")
    props_qs = Property.objects.all().iterator(chunk_size=2000)
    props_csv_path = os.path.join(out_dir, "properties_summary.csv")
    props_fieldnames = [
        "id", "slug", "address", "city", "state", "zip_code", "bedrooms",
        "bathrooms", "sqft", "monthly_rent_dollars", "status", "is_published",
        "pets_allowed", "voucher_accepted"
    ]
    prop_count = 0
    with open(props_csv_path, "w", newline="", encoding="utf-8") as f_csv:
        writer = csv.DictWriter(f_csv, fieldnames=props_fieldnames)
        writer.writeheader()
        for p in props_qs:
            prop_count += 1
            baths = p.half_bathrooms / 2.0 if p.half_bathrooms else 0.0
            writer.writerow({
                "id": str(p.id),
                "slug": p.slug,
                "address": p.address,
                "city": p.city,
                "state": p.state,
                "zip_code": p.zip_code,
                "bedrooms": p.bedrooms,
                "bathrooms": baths,
                "sqft": p.sqft,
                "monthly_rent_dollars": cents_to_dollars(p.price_cents),
                "status": p.status,
                "is_published": p.is_published,
                "pets_allowed": p.pets_allowed,
                "voucher_accepted": p.voucher_accepted,
            })
    print(f"  -> Exported {prop_count} properties summary.")

    # =========================================================================
    # 9. AGGREGATES & METRICS SUMMARY (Pre-computed KPIs)
    # =========================================================================
    print("Computing Analytics Aggregates & KPIs...")
    
    # Device breakdown
    device_counts = dict(VisitorSession.objects.exclude(device_type="").values("device_type").annotate(count=Count("id")).order_by("-count"))
    
    # OS breakdown
    os_counts = dict(VisitorSession.objects.exclude(os="").values("os").annotate(count=Count("id")).order_by("-count")[:15])
    
    # Browser breakdown
    browser_counts = dict(VisitorSession.objects.exclude(browser="").values("browser").annotate(count=Count("id")).order_by("-count")[:15])
    
    # Top Cities
    top_cities = [
        {"city": r["city"], "country": r["country"], "sessions": r["count"]}
        for r in VisitorSession.objects.exclude(city="").values("city", "country").annotate(count=Count("id")).order_by("-count")[:25]
    ]
    
    # Top Regions / States
    top_regions = [
        {"region": r["region"], "country": r["country"], "sessions": r["count"]}
        for r in VisitorSession.objects.exclude(region="").values("region", "country").annotate(count=Count("id")).order_by("-count")[:25]
    ]
    
    # Top Landing Pages
    top_landing_pages = [
        {"landing_page": r["landing_page"], "sessions": r["count"]}
        for r in VisitorSession.objects.exclude(landing_page="").values("landing_page").annotate(count=Count("id")).order_by("-count")[:25]
    ]
    
    # Top Referrers
    top_referrers = [
        {"referrer": r["referrer"], "sessions": r["count"]}
        for r in VisitorSession.objects.exclude(referrer="").values("referrer").annotate(count=Count("id")).order_by("-count")[:25]
    ]
    
    # Top Page Visit Paths
    top_pages_visited = [
        {"path": r["path"], "visits": r["count"], "avg_scroll": round(r["avg_scroll"] or 0, 1)}
        for r in PageVisit.objects.values("path").annotate(count=Count("id"), avg_scroll=Avg("max_scroll_depth")).order_by("-count")[:30]
    ]
    
    # Top Telemetry Event Types
    top_event_types = [
        {"event_type": r["event_type"], "count": r["count"]}
        for r in TelemetryEvent.objects.values("event_type").annotate(count=Count("id")).order_by("-count")[:30]
    ]

    # Application Statuses
    app_status_counts = dict(RentalApplication.objects.values("status").annotate(count=Count("id")).order_by("-count"))
    
    # Lead Statuses & Sources
    lead_status_counts = dict(Lead.objects.values("status").annotate(count=Count("id")).order_by("-count"))
    lead_source_counts = dict(Lead.objects.values("source").annotate(count=Count("id")).order_by("-count"))
    
    # Tour Request Statuses
    tour_status_counts = dict(TourRequest.objects.values("status").annotate(count=Count("id")).order_by("-count"))
    
    # Payment Totals
    total_payments = Payment.objects.count()
    verified_payments_cents = Payment.objects.filter(status="VERIFIED").aggregate(sum=Sum("amount_cents"))["sum"] or 0
    payment_methods_counts = dict(Payment.objects.values("payment_method").annotate(count=Count("id")).order_by("-count"))

    summary = {
        "generated_at": datetime.now().isoformat(),
        "totals": {
            "visitors": visitor_count,
            "visitor_sessions": session_count,
            "page_visits": page_count,
            "telemetry_events": event_count,
            "rental_applications": len(apps_data_list),
            "leads": len(leads_data_list),
            "tour_requests": len(tours_data_list),
            "payments_recorded": total_payments,
            "total_verified_revenue_dollars": cents_to_dollars(verified_payments_cents),
        },
        "conversion_funnel": {
            "unique_visitors": visitor_count,
            "total_sessions": session_count,
            "leads_captured": len(leads_data_list),
            "tours_requested": len(tours_data_list),
            "applications_initiated": len(apps_data_list),
            "applications_submitted": RentalApplication.objects.exclude(status="DRAFT").count(),
            "applications_fee_paid": RentalApplication.objects.filter(is_fee_paid=True).count(),
            "applications_approved": RentalApplication.objects.filter(status__in=["APPROVED", "APPROVED_WITH_CONDITIONS"]).count(),
            "leases_sent": RentalApplication.objects.filter(lease_sent_at__isnull=False).count(),
            "leases_signed": RentalApplication.objects.filter(lease_signed_at__isnull=False).count(),
        },
        "devices": device_counts,
        "operating_systems": os_counts,
        "browsers": browser_counts,
        "top_cities": top_cities,
        "top_regions": top_regions,
        "top_landing_pages": top_landing_pages,
        "top_referrers": top_referrers,
        "top_pages_visited": top_pages_visited,
        "top_telemetry_events": top_event_types,
        "applications_by_status": app_status_counts,
        "leads_by_status": lead_status_counts,
        "leads_by_source": lead_source_counts,
        "tours_by_status": tour_status_counts,
        "payments_by_method": payment_methods_counts,
    }

    summary_json_path = os.path.join(out_dir, "analytics_summary.json")
    with open(summary_json_path, "w", encoding="utf-8") as f_summary:
        json.dump(summary, f_summary, indent=2, default=json_serial)

    print(f"[{datetime.now().isoformat()}] Extraction and summary complete!")
    print(f"Generated files in {out_dir}:")
    for fname in os.listdir(out_dir):
        sz = os.path.getsize(os.path.join(out_dir, fname))
        print(f"  - {fname} ({sz / 1024:.1f} KB)")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/srg_analytics_export"
    export_all(out)
