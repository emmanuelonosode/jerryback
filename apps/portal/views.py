"""
Resident portal endpoints: maintenance tickets and documents.

EVERY QUERYSET STARTS FROM THE REQUESTING USER, NOT FROM THE MODEL.

There is one authorisation rule in this file and it is applied the same way
everywhere: resolve the caller's Client row first, then filter by it. No view
here accepts a client id, a property id, or any other ownership hint from the
request — an endpoint that trusts a caller-supplied owner is an IDOR, and on
this data that means one resident reading another's lease, their identity
documents, and the inside of their home.

Residents who have registered but are not yet a Client (no application yet) get
an empty list rather than a 403. They are legitimately authenticated and simply
have nothing here; a 403 would read as "you are not allowed" and generate a
support call.
"""

from django.db.models import Case, IntegerField, When
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import ClientDocument, MaintenancePriority, MaintenanceRequest, MaintenanceStatus
from .serializers import (
    ClientDocumentSerializer,
    MaintenanceRequestCreateSerializer,
    MaintenanceRequestSerializer,
)

# Severity as a number, because the model's `-priority` sorts the *string*.
#
# Descending alphabetically that is URGENT, MEDIUM, LOW, HIGH — so a HIGH
# ticket ("significant disruption to daily living") lands below a LOW one
# ("routine, non-urgent") on a queue whose comment says "urgent first". Ranking
# explicitly is the only way this ordering means what it claims.
PRIORITY_RANK = Case(
    When(priority=MaintenancePriority.URGENT, then=0),
    When(priority=MaintenancePriority.HIGH, then=1),
    When(priority=MaintenancePriority.MEDIUM, then=2),
    When(priority=MaintenancePriority.LOW, then=3),
    default=4,
    output_field=IntegerField(),
)

OPEN_STATUSES = [
    MaintenanceStatus.SUBMITTED,
    MaintenanceStatus.ACKNOWLEDGED,
    MaintenanceStatus.IN_PROGRESS,
]


def _client_for(user):
    """The caller's Client row, or None if they have not become one yet."""
    from apps.crm.models import Client

    return Client.objects.filter(user=user).first()


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def maintenance(request):
    client = _client_for(request.user)

    if request.method == "GET":
        if client is None:
            return Response([])

        queryset = (
            MaintenanceRequest.objects.filter(client=client)
            .select_related("property")
            .annotate(rank=PRIORITY_RANK)
            .order_by("rank", "-created_at")
        )

        state = request.query_params.get("state")
        if state == "active":
            queryset = queryset.filter(status__in=OPEN_STATUSES)
        elif state == "resolved":
            queryset = queryset.exclude(status__in=OPEN_STATUSES)

        return Response(MaintenanceRequestSerializer(queryset, many=True).data)

    # POST — raising a ticket needs somewhere to attach it.
    if client is None:
        return Response(
            {"detail": "Maintenance requests are for residents with an active application or lease."},
            status=status.HTTP_403_FORBIDDEN,
        )

    serializer = MaintenanceRequestCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    # `client` comes from the session, never from the payload.
    ticket = serializer.save(client=client)

    return Response(
        MaintenanceRequestSerializer(ticket).data,
        status=status.HTTP_201_CREATED,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_documents(request):
    client = _client_for(request.user)
    if client is None:
        return Response([])

    queryset = ClientDocument.objects.filter(client=client)

    document_type = request.query_params.get("type")
    if document_type:
        queryset = queryset.filter(document_type=document_type.upper())

    return Response(ClientDocumentSerializer(queryset, many=True).data)
