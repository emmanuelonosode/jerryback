from django.contrib import admin
from unfold.admin import ModelAdmin as UnfoldModelAdmin, TabularInline as UnfoldTabularInline, StackedInline as UnfoldStackedInline

from .models import TourRequest, Viewing

@admin.register(TourRequest)
class TourRequestAdmin(UnfoldModelAdmin):
    pass

@admin.register(Viewing)
class ViewingAdmin(UnfoldModelAdmin):
    pass

