from django.urls import path

from . import views

urlpatterns = [
    # Resident portal — authenticated, and declared before the slug route so
    # "favorites" is never mistaken for a property slug.
    path("favorites/", views.favorites, name="favorites"),
    path("favorites/toggle/", views.toggle_favorite, name="toggle-favorite"),
    path("favorites/merge/", views.merge_favorites, name="merge-favorites"),
    path("favorites/<uuid:favorite_id>/", views.remove_favorite, name="remove-favorite"),
    # Public catalogue.
    path("", views.inventory, name="inventory"),
    path("cities/", views.inventory_cities, name="inventory-cities"),
    path("map_pins/", views.inventory_map_pins, name="inventory-map-pins"),
    path("<slug:slug>/", views.inventory_detail, name="inventory-detail"),
]
