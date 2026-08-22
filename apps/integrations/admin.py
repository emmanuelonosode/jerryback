from django.contrib import admin
from unfold.admin import ModelAdmin as UnfoldModelAdmin, TabularInline as UnfoldTabularInline, StackedInline as UnfoldStackedInline

from .models import OutboundEmail

@admin.register(OutboundEmail)
class OutboundEmailAdmin(UnfoldModelAdmin):
    pass

