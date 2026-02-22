from __future__ import annotations

from django.urls import path

from library import views

app_name = "library"

urlpatterns = [
    path("cover/<int:album_id>/", views.cover_art, name="cover_art"),
]
