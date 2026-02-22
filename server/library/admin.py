from django.contrib import admin

from library.models import Album, Artist, Track


@admin.register(Artist)
class ArtistAdmin(admin.ModelAdmin):
    list_display = ["name", "sort_name"]
    search_fields = ["name"]


@admin.register(Album)
class AlbumAdmin(admin.ModelAdmin):
    list_display = ["title", "artist", "year", "total_tracks"]
    list_filter = ["year"]
    search_fields = ["title", "artist__name"]


@admin.register(Track)
class TrackAdmin(admin.ModelAdmin):
    list_display = ["title", "artist", "album", "track_number", "genre", "format", "duration"]
    list_filter = ["format", "genre"]
    search_fields = ["title", "artist__name", "album__title"]
