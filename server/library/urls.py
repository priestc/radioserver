from __future__ import annotations

from django.urls import path

from library import views

app_name = "library"

urlpatterns = [
    path("cover/<int:album_id>/", views.cover_art, name="cover_art"),
    path("api/client_sync/", views.client_sync, name="client_sync"),
    path("api/download_song/<int:playlist_item_id>/", views.download_song, name="download_song"),
    path("api/download_song_lowbitrate/<int:playlist_item_id>/", views.download_song_lowbitrate, name="download_song_lowbitrate"),
    path("api/tracks/", views.search_tracks, name="search_tracks"),
    path("api/tracks/<int:track_id>/download/", views.download_track, name="download_track"),
]
