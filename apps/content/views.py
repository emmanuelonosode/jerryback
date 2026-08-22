"""
Staff hiring endpoints.

GATED ON AN ENUMERATED PERMISSION, not on `is_staff` and not on a role-name
comparison. HIRING_READ and HIRING_MANAGE are granted to ADMIN and MANAGER in
the grant table, which is where changing that access is a visible, reviewable
edit rather than a condition buried in a view.
"""

from django.db.models import Q
from rest_framework import status as http
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.accounts.permissions import HIRING_MANAGE, HIRING_READ, HasPermission

from .models import JobApplication
from .serializers import JobApplicationSerializer


@api_view(["GET"])
@permission_classes([HasPermission.of(HIRING_READ)])
def job_applications(request):
    queryset = JobApplication.objects.all()

    search = request.query_params.get("search", "").strip()
    if search:
        queryset = queryset.filter(
            Q(full_name__icontains=search)
            | Q(email__icontains=search)
            | Q(phone__icontains=search),
        )

    state = request.query_params.get("status", "").strip().upper()
    if state:
        queryset = queryset.filter(status=state)

    role = request.query_params.get("role", "").strip()
    if role:
        queryset = queryset.filter(role_title__iexact=role)

    return Response(JobApplicationSerializer(queryset, many=True).data)


@api_view(["PATCH"])
@permission_classes([HasPermission.of(HIRING_MANAGE)])
def job_application_detail(request, application_id):
    record = JobApplication.objects.filter(id=application_id).first()
    if record is None:
        return Response({"detail": "No such application."}, status=http.HTTP_404_NOT_FOUND)

    serializer = JobApplicationSerializer(record, data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)
    # Who last touched it, taken from the session rather than the payload.
    serializer.save(reviewed_by=request.user)
    return Response(serializer.data)


@api_view(["POST"])
@permission_classes([])
def submit_job_application(request):
    """Public job application submission."""
    from rest_framework.permissions import AllowAny
    data = request.data or {}
    serializer = JobApplicationSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    record = serializer.save()
    return Response(JobApplicationSerializer(record).data, status=http.HTTP_201_CREATED)

