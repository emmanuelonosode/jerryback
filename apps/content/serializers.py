from rest_framework import serializers

from .models import JobApplication, JobApplicationStatus


class JobApplicationSerializer(serializers.ModelSerializer):
    """
    Staff view of a candidate.

    A CANDIDATE RECORD IS AN OUTSIDER'S PERSONAL DATA — a name, a phone number,
    and a piece of writing supplied to get a job, not to be browsed. Everything
    the candidate wrote is read-only here; the only writable fields are the ones
    that represent a staff decision.
    """

    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = JobApplication
        fields = [
            "id", "role_id", "role_title", "full_name", "email", "phone",
            "linkedin_url", "motivation", "questionnaire",
            "status", "status_display", "staff_notes", "interview_date",
            "applied_at",
        ]
        read_only_fields = [
            "id", "role_id", "role_title", "full_name", "email", "phone",
            "linkedin_url", "motivation", "questionnaire",
            "status_display", "applied_at",
        ]

    def validate(self, attrs):
        status = attrs.get("status", getattr(self.instance, "status", None))
        interview = attrs.get(
            "interview_date", getattr(self.instance, "interview_date", None),
        )
        # An interview marked scheduled with no date is a state nobody can act
        # on, and it is exactly what a half-filled form produces.
        if status == JobApplicationStatus.INTERVIEW_SCHEDULED and interview is None:
            raise serializers.ValidationError(
                {"interview_date": "Pick a date and time before marking an interview scheduled."},
            )
        return attrs
