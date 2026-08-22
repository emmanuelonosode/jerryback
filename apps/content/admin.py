from django.contrib import admin
from unfold.admin import ModelAdmin as UnfoldModelAdmin, TabularInline as UnfoldTabularInline, StackedInline as UnfoldStackedInline

from .models import JobApplication, Post

@admin.register(JobApplication)
class JobApplicationAdmin(UnfoldModelAdmin):
    pass

@admin.register(Post)
class PostAdmin(UnfoldModelAdmin):
    pass

