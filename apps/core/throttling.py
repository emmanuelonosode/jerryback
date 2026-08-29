"""
Throttling that does not throttle our own web server.

THE OUTAGE THIS FIXES. The public site renders on the server: a page view of a
city hub makes one or more calls to this API from the Next.js process, which
sits on this same host. To DRF every one of those looks like an anonymous
browser client on a single IP, so all of them share one `anon` bucket of
120 requests a minute.

That bucket belongs to the entire site, not to a visitor. A crawl of the 4,840
URLs in the sitemap - which is exactly what we are asking Google to do -
exhausts it in seconds, and the frontend then renders 500 for every subsequent
page because it refuses to serve fixture data in production. Measured: a
twelve-way parallel sweep of the hub pages produced 104 failures, every one of
them an HTTP 429 from here.

Raising the number would only move the wall. The right answer is that our own
web server is not an anonymous member of the public: it is the thing serving
the public, and its request rate is a function of how many people are reading
the site. Real anonymous clients - somebody scripting against the API from
outside - are still capped exactly as before.

DELIBERATELY NOT AN AUTH BYPASS. This changes rate limiting only. Permission
classes, authentication and every `IsAuthenticated` endpoint behave
identically; an exempt IP still cannot read anything a stranger could not.
"""

from __future__ import annotations

import ipaddress

from django.conf import settings
from rest_framework.throttling import AnonRateThrottle


def _networks() -> list[ipaddress._BaseNetwork]:
    """
    Loopback, plus whatever `INTERNAL_API_IPS` names.

    Loopback is always trusted: a request arriving on 127.0.0.1 came from this
    machine. Everything else has to be named in the environment, because the
    public address of this host is not something to infer.
    """
    nets: list[ipaddress._BaseNetwork] = [
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("::1/128"),
    ]
    for raw in getattr(settings, "INTERNAL_API_IPS", []):
        candidate = str(raw).strip()
        if not candidate:
            continue
        try:
            nets.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            # A malformed entry is ignored rather than fatal: a typo in an env
            # var must not take the API down, it must only fail to exempt.
            continue
    return nets


class InternalExemptAnonThrottle(AnonRateThrottle):
    """`anon` rate for the public, no limit for this host's own renderer."""

    def allow_request(self, request, view):
        ident = self.get_ident(request)
        if ident:
            try:
                address = ipaddress.ip_address(ident.split(",")[0].strip())
            except ValueError:
                address = None
            if address is not None and any(address in net for net in _networks()):
                return True
        return super().allow_request(request, view)
