"""Careers and the blog CMS."""

import uuid

from django.db import models
from django.utils.text import slugify


class JobApplicationStatus(models.TextChoices):
    SUBMITTED = "SUBMITTED", "Submitted"
    UNDER_REVIEW = "UNDER_REVIEW", "Under review"
    INTERVIEW_SCHEDULED = "INTERVIEW_SCHEDULED", "Interview scheduled"
    HIRED = "HIRED", "Hired"
    REJECTED = "REJECTED", "Rejected"


class JobApplication(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role_id = models.CharField(max_length=60, blank=True, default="")
    role_title = models.CharField(max_length=200)
    full_name = models.CharField(max_length=200)
    email = models.EmailField()
    phone = models.CharField(max_length=20, blank=True, default="")
    linkedin_url = models.URLField(max_length=500, blank=True, default="")
    motivation = models.TextField(blank=True, default="")
    # [{question, answer}] — role-specific, so the shape is not fixed here.
    questionnaire = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=24, choices=JobApplicationStatus.choices, default=JobApplicationStatus.SUBMITTED,
    )
    applied_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL)
    staff_notes = models.TextField(blank=True, default="")
    interview_date = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "job_applications"
        ordering = ["-applied_at"]


class PostCategory(models.TextChoices):
    RENTER_GUIDE = "RENTER_GUIDE", "Renter guide"
    MARKET_UPDATE = "MARKET_UPDATE", "Market update"
    MARKET_ANALYSIS = "MARKET_ANALYSIS", "Market analysis"
    BUYERS_GUIDE = "BUYERS_GUIDE", "Buyer's guide"
    SELLERS_GUIDE = "SELLERS_GUIDE", "Seller's guide"
    INVESTMENT = "INVESTMENT", "Investment"


class Post(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=250, unique=True)
    excerpt = models.TextField(blank=True, default="")
    content = models.TextField()
    category = models.CharField(max_length=20, choices=PostCategory.choices)
    author = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL)
    featured_image_url = models.CharField(max_length=500, blank=True, default="")
    is_published = models.BooleanField(default=False, db_index=True)
    is_featured = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    tags = models.JSONField(default=list, blank=True)
    read_time_minutes = models.PositiveSmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "posts"
        ordering = ["-published_at", "-created_at"]

    def __str__(self) -> str:
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.title) or "post"
            candidate, n = base, 2
            while Post.objects.filter(slug=candidate).exclude(pk=self.pk).exists():
                candidate, n = f"{base}-{n}", n + 1
            self.slug = candidate
        if not self.read_time_minutes and self.content:
            # ~220 words per minute, floor of one.
            self.read_time_minutes = max(1, round(len(self.content.split()) / 220))
        super().save(*args, **kwargs)
