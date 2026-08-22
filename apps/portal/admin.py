from django.contrib import admin
from unfold.admin import ModelAdmin as UnfoldModelAdmin, TabularInline as UnfoldTabularInline, StackedInline as UnfoldStackedInline

from .models import ClientDocument, MaintenanceRequest

@admin.register(ClientDocument)
class ClientDocumentAdmin(UnfoldModelAdmin):
    pass

@admin.register(MaintenanceRequest)
class MaintenanceRequestAdmin(UnfoldModelAdmin):
    pass

