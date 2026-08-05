from __future__ import annotations

from django.urls import path

from library import views

app_name = "library"

urlpatterns = [
    path("cover/<int:album_id>/", views.cover_art, name="cover_art"),
    path("api/client_sync/", views.client_sync, name="client_sync"),
    path("api/download_song/<int:playlist_item_id>/", views.download_song, name="download_song"),
    path("api/download_song_lowbitrate/<int:playlist_item_id>/", views.download_song_lowbitrate, name="download_song_lowbitrate"),
    path("api/channels/", views.list_channels, name="list_channels"),
    path("api/decade/<slug:decade_slug>/stations/", views.decade_stations, name="decade_stations"),
    path("api/decade/<slug:decade_slug>/station/<slug:station_slug>/sync/", views.decade_station_sync, name="decade_station_sync"),
    path("api/tracks/", views.search_tracks, name="search_tracks"),
    path("api/tracks/<int:track_id>/download/", views.download_track, name="download_track"),
    path("api/autocomplete/artists/", views.autocomplete_artists, name="autocomplete_artists"),
    path("api/autocomplete/titles/", views.autocomplete_titles, name="autocomplete_titles"),
    path("api/video_channels/", views.list_video_channels, name="list_video_channels"),
    path("browse/", views.browse_page, name="browse_page"),
    path("api/browse/albums/", views.browse_albums, name="browse_albums"),
    path("api/browse/albums/<int:album_id>/", views.browse_album_detail, name="browse_album_detail"),
    path("api/browse/artists/", views.browse_artists, name="browse_artists"),
    path("api/browse/artists/<int:artist_id>/", views.browse_artist_detail, name="browse_artist_detail"),
    path("api/browse/genres/", views.browse_genres, name="browse_genres"),
    path("api/browse/genres/<str:genre>/tracks/", views.browse_genre_mix, name="browse_genre_mix"),
    path("api/browse/stream/<int:track_id>/", views.browse_stream_track, name="browse_stream_track"),
    path("api/browse/search/", views.browse_search, name="browse_search"),
    path("video_frame/<int:video_channel_id>/<int:frame_number>/", views.video_frame, name="video_frame"),
    path("video_audio/<int:video_channel_id>/", views.video_audio, name="video_audio"),
]
