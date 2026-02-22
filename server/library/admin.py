from django.contrib import admin

from library.models import Album, Artist, GenreGroup, PlaylistSettings, Track


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


@admin.register(GenreGroup)
class GenreGroupAdmin(admin.ModelAdmin):
    list_display = ["name", "genres"]
    search_fields = ["name", "genres"]


@admin.register(PlaylistSettings)
class PlaylistSettingsAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj, _ = PlaylistSettings.objects.get_or_create(pk=1)
        from django.shortcuts import redirect
        return redirect(f"../playlistsettings/{obj.pk}/change/")
