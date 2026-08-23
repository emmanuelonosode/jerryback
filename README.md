# Skelton Realty Group - admin & API

A standalone Django service, deployed at **admin.skeltonrealtygroup.com**, separate
from the public Next.js marketing site at `skeltonrealtygroup.com`.

It is two things at once:

- **The staff admin.** Django's admin, at the root of this host, for inventory,
  leads, applications, payments and content.
- **The API.** Read-only public inventory endpoints the marketing site consumes,
  plus authenticated endpoints for applicants and residents.

---

## Why a separate host, and what follows from it

Keeping this off the public domain is not cosmetic. Three settings depend on it:

**Cookies are host-only.** `SESSION_COOKIE_DOMAIN` is deliberately unset, so the
staff session cookie is scoped to `admin.skeltonrealtygroup.com` and nowhere
else. Had it been set to `.skeltonrealtygroup.com`, the cookie would be sent to
the public marketing site on every request, and any XSS anywhere on that site
would become a staff session takeover.

**CORS is an exact allowlist with credentials enabled.** Never a wildcard: with
credentials allowed, a wildcard lets any site drive this API using a logged-in
staff member's browser.

**The admin can be moved off `/admin/`** with `ADMIN_PATH`. Not security on its
own - it takes this host out of the noise floor of bots probing the default path.

---

## The admin theme

[django-unfold](https://unfoldadmin.com/) 0.104, configured in `settings.UNFOLD`.

Three things about it are worth knowing before editing:

**`unfold` must precede `django.contrib.admin` in INSTALLED_APPS.** It overrides
the admin templates and Django resolves them in app order - listed after, the
stock templates win and the theme silently does nothing.

**Every admin class inherits from `unfold.admin.ModelAdmin`,** not Django's. A
plain `admin.site.register(Model)` renders unstyled inside an otherwise themed
shell, which reads as half-broken rather than as a deliberate look.

**Unfold ships PRECOMPILED Tailwind.** Utility classes it does not itself use
are simply absent from the stylesheet, so writing `dark:bg-green-500/10` in a
template produces a class that resolves to nothing - silently, with the markup
looking correct. Dashboard styling therefore lives in
`static/srg/css/dashboard.css` as plain CSS keyed off `.dark`, registered via
`UNFOLD["STYLES"]`. Note the path: a file at `static/admin/css/…` loses to
`django.contrib.admin`'s own copy of that path.

### The landing page is a dashboard, not the app list

`templates/admin/index.html` replaces Django's index. The sidebar already groups
the site by job, so a model index would duplicate every entry and reintroduce
the raw app labels ("Crm", "Portal") the sidebar exists to avoid. It answers the
arrival question instead - is anything late? - from `config/dashboard.py`.

The sidebar carries live badges on the queues where a delay is a broken promise
to a real person: an application past its deadline, money already sent and
waiting on a person, an identity document blocking a viewing. A queue nobody can
see is a queue nobody works.

---

## Running it locally

```bash
python3.13 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env          # then edit it

./.venv/bin/python manage.py migrate
./.venv/bin/python manage.py create_staff \
    --email you@skeltonrealtygroup.com --first-name Your --last-name Name --role ADMIN
DEBUG=True ./.venv/bin/python manage.py runserver 8000
```

SQLite is the default locally. Postgres is used in production via `DATABASE_URL`,
and `docker compose up` runs the production-shaped stack.

### Tests

```bash
./.venv/bin/python manage.py test
```

120 tests. They run against real settings with two exceptions, both in
`settings.TESTING`: the HTTPS redirect is off (otherwise every request 301s
before reaching a view) and password hashing is fast (Argon2 is correct in
production and makes a suite crawl; the hashing behaviour has its own tests).

---

## Deploying to admin.skeltonrealtygroup.com

### 1. DNS

An `A`/`AAAA` record for `admin` pointing at the host. This is a distinct origin
from the marketing site - do not CNAME it to the same Vercel/Netlify deployment.

### 2. Environment

Copy `.env.example` and fill it in. Generate the two secrets separately:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

`SECRET_KEY` and `JWT_SECRET` are separate on purpose: rotating session signing
should not invalidate every API token at the same moment, and vice versa.

### 3. TLS and the reverse proxy

```nginx
server {
    listen 443 ssl http2;
    server_name admin.skeltonrealtygroup.com;

    ssl_certificate     /etc/letsencrypt/live/admin.skeltonrealtygroup.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/admin.skeltonrealtygroup.com/privkey.pem;

    # Identity documents and proof-of-payment images are uploaded here.
    client_max_body_size 12M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        # Django reads this to know the original request was HTTPS. Without it
        # SECURE_SSL_REDIRECT sees http and redirects forever.
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Serve uploads directly rather than through Python.
    location /media/ { alias /var/www/srg-admin/media/; }
}

server {
    listen 80;
    server_name admin.skeltonrealtygroup.com;
    return 301 https://$host$request_uri;
}
```

### 4. Run it

```bash
docker compose up -d --build          # or gunicorn directly, see the Dockerfile
```

Migrations run on container start rather than at build time: the build has no
database, and running them per-deploy keeps schema and code moving together.

### 5. Point the public site at it

On the Next.js side, set the API base URL to
`https://admin.skeltonrealtygroup.com/api/v1` and add the marketing origins to
`CORS_ALLOWED_ORIGINS` here.

---

## The database

The engine comes from `DATABASE_URL`, not from code. SQLite is only a fallback
so a fresh checkout runs with nothing installed — it is not a production
target.

```
mysql://user:password@host:3306/srg_admin        # MySQL 8.0.16+
postgresql://user:password@host:5432/srg_admin
```

Nothing here is Postgres-specific: no `django.contrib.postgres` imports, no
array or range fields, no search vectors. MySQL is a supported target rather
than a port, with two requirements.

**MySQL must be 8.0.16 or newer.** Fourteen tables carry CHECK constraints and
older MySQL parses them, then silently ignores them. They are not decoration —
they are what stops a payment of zero, a second primary image on one property,
and a payment method going live with nothing to pay to. On an older server
those rules quietly stop applying and nothing tells you.

**utf8mb4 and `STRICT_TRANS_TABLES`** are set automatically in `settings.py`
when the engine is MySQL. Strict mode matters: without it MySQL shortens an
over-long value and writes it anyway rather than raising.

Sizes are within MySQL's limits — the widest indexed column is
`Property.search_text` at 500 chars, 2000 bytes in utf8mb4, against InnoDB's
3072-byte index limit.

Two differences worth knowing rather than fixing:

- **UUID primary keys** (31 of them) are stored as `char(32)` on MySQL, where
  Postgres has a native 16-byte `uuid`. Slightly larger indexes; not a problem
  at this scale.
- **`LIKE` is case-insensitive** under MySQL's default collation and
  case-sensitive on Postgres. Property search is unaffected either way because
  it normalises both the stored haystack and the query to lowercase before
  matching — see `normalise_search_text`.

### Driver

`psycopg` for Postgres. For MySQL, `mysqlclient` is faster but needs the C
client at build time; `PyMySQL` is pure Python and `settings.py` falls back to
it automatically when `mysqlclient` cannot be imported.

```bash
brew install mysql-client && pip install mysqlclient   # optional, faster
```

### Moving the data

There is no data worth migrating yet — the current SQLite file is development
inventory. Point `DATABASE_URL` at MySQL and run:

```bash
python manage.py migrate
python manage.py createsuperuser
python manage.py rebuild_search_index
```

If you do need to carry data across, `dumpdata`/`loaddata` works for a set this
size; do not copy the SQLite file.

## Scheduled jobs

Mail that somebody is waiting on — a verification code, an approval — is now
sent **immediately**, inside the request that triggers it. The queue behind it
is a retry net, not the delivery path: if the mail server is slow or down, the
send fails quietly, the row stays queued, and cron picks it up. So a code
arrives in a second, and a broken mail server still cannot fail a registration.

Cron lines, for CloudPanel or crontab. Use absolute paths — cron has almost no
environment:

```cron
* * * * *   cd /home/backend/htdocs/admin.skeltonrealtygroup.com && .venv/bin/python manage.py send_queued_email  >> /var/log/srg-mail.log 2>&1
*/5 * * * * cd /home/backend/htdocs/admin.skeltonrealtygroup.com && .venv/bin/python manage.py process_telemetry   >> /var/log/srg-telemetry.log 2>&1
```

Overlapping runs are safe. `send_queued_email` claims each message with a
compare-and-swap before sending, so a second run started while the first is
still working skips what is already claimed rather than sending it twice, and
anything left claimed by a run that died is recovered after fifteen minutes.

`process_telemetry` handles 200 events per run by default. A backlog builds
quietly if it is not scheduled — 3,326 events had accumulated before this was
written — and the admin then shows stale visitor data with nothing to say why.



Three, and the service is quietly broken without the first two.

| Command | Cadence | What breaks without it |
| --- | --- | --- |
| `python manage.py send_queued_email` | 1–2 min | **Nothing is emailed at all.** Verification codes and approval notices queue in `OutboundEmail` and sit there. |
| `python manage.py process_telemetry` | few min | Analytics events pile up unprocessed in `RawTelemetryEvent`. |
| `python manage.py rebuild_search_index` | after each feed import | `bulk_create` bypasses `save()`, so imported homes are invisible to search while visible to every filter. |

## Payment rails

At least one must be active in **Billing → Payment method configs**, or the
application fee cannot be paid and no application can be completed.

```bash
python manage.py seed_demo_payment_methods              # all twelve, fake details
python manage.py seed_demo_payment_methods --deactivate
```

The seeded values are deliberately implausible — reserved 555 numbers, a
`.test` domain, all-zero routing numbers, wallet addresses that decode to
nothing. A realistic-looking demo account number is one deploy away from being
the number a real applicant sends rent to. **Replace every one by hand before
launch.**

A payment declared on the public application form arrives in the admin queue as
`PENDING_VERIFICATION`. Verifying it marks the application's fee paid and starts
the 24-hour decision clock — that transition is the one an applicant sees.

## Housekeeping

```bash
python manage.py purge_rent_restatement_fees --dry-run
```

Partner feeds ship a monthly fee row labelled "Base Monthly Rent" whose amount
*is* the rent. The API excludes them, so totals are correct without this, but
they remain visible in the admin where a fee list shows the rent twice. They
come back on the next sync unless the importer is fixed too.

## Where the implementation departs from the supplied spec

Each of these is a security or correctness decision, not a preference. They are
listed here because the next person to read the spec will notice something
missing and wonder.

### `raw_password_encrypted` does not exist

The spec stores the user's password reversibly encrypted *alongside* its hash.
That defeats hashing entirely: anyone reaching the database and the server
secret recovers every plaintext password, and because people reuse passwords the
damage lands on their email and their bank, not on this site. No feature needs
it - a reset issues a new credential rather than recovering the old one.

### The full SSN is not stored, encrypted or otherwise

The spec derives a Fernet key from `SECRET_KEY` via SHA-256. The key is
therefore the secret: one config leak, backup, error page or git history hands
over every SSN in the table. That is encryption at rest against a threat model
where the attacker never has the application config.

And nothing needs it. The number exists to reach a screening vendor, which
returns a report reference; the decision is made from the report. So:
`ssn_last4` for identification, `screening_reference` for the report, and the
full number passes through memory and is never written down.

### `payments` carries no card columns

`card_number`, `card_expiry`, `cardholder_name` and `billing_address` would pull
this service into PCI DSS scope, which is exactly what the manual-rails decision
was made to avoid. If card payment is added, a processor returns a token and the
token is what lands here.

### `lead_score` is computed, not stored

The formula reads activity count, age in days and status, so a stored value is
wrong the moment a day passes. It is a model property.

It also ranks *who to call first* and must never gate qualification - screening
runs against the published two-tier criteria applied consistently, which is the
Fair Housing safe harbour. Its inputs are confined to intent and engagement:
household size and pets score for being **answered**, not for their value, so a
family of five scores exactly like a single occupant.

### Roles are a grant table, not a hierarchy

`ADMIN > MANAGER > AGENT > ACCOUNTANT` reads well and is wrong: an accountant
should verify payments and never see an applicant's date of birth; an agent
should read the applicant and never mark money received. A rank forces one of
those to be wrong. See `apps/accounts/permissions.py`.

### Money is integer cents, not `DecimalField`

Python's `Decimal` is exact, so the usual argument does not apply. This is about
having **one** representation: the public site's money model is integer cents end
to end, and emitting `"3345.00"` would mean the frontend parses it back - which
is exactly where money bugs live.

### Fields added that the spec omits

`Property.voucher_accepted` and `Lead.has_voucher`. Voucher acceptance is a
search filter, a landing page, and a promise repeated on every page of the
public site. It is absent from the supplied spec *and* from both partner feeds,
so it is maintained here.

---

## Invariants enforced by the database, not by application code

Service code that "unsets the other primary image first" is correct until two
requests race, a bulk import writes directly, or someone fixes data in a shell.
These are constraints:

| Constraint | What it prevents |
|---|---|
| `one_primary_image_per_property` | A property with two primary photos, or none |
| `leased_property_has_leased_at` | A leased home with no date, so its 45-day grace window never expires |
| `conditional_fee_states_when` | A fee a renter cannot tell whether they will be charged |
| `active_method_has_details` | A live payment method with nothing to pay to |
| `decided_payment_records_actor` | A verified payment with no answer to "who marked this paid" |
| `rejected_payment_has_reason` | "Rejected", unexplained, about money already sent |
| `payment_amount_positive` | A zero or negative payment |

---

## Things that still need real values

Nothing in this service invents a business number. `.env.example` carries
placeholders for the application fee, the lease admin fee and the decision
window; the real figures come from the business, and the public site's launch
gate blocks on the same list.

Also outstanding: the object-storage adapter (`STORAGE_BACKEND` currently only
implements `local`), the SMTP worker that drains `outbound_emails`, PDF
generation for applications and receipts, and the scheduled jobs (telemetry
spool processing, invoice reminders, viewing reminders, abandoned-draft
recovery, identity-document purging). The models and queries they need are in
place - see `apps/scheduler/models.py` for `ready_to_purge` and
`needing_reminder`, and `apps/crm/services.py` for `overdue_applications` and
`abandoned_drafts`.
# jerryback
