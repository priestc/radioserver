from pathlib import Path

from django import forms
from django.contrib import admin
from django.utils.html import format_html

from library.models import Album, Artist, GenreGroup, PlaylistSettings, Track


class GenreGroupForm(forms.ModelForm):
    genre_choices = forms.MultipleChoiceField(
        choices=[],
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Genres",
    )

    class Meta:
        model = GenreGroup
        fields = ["name"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Collect genres already claimed by other genre groups
        taken = set()
        for gg in GenreGroup.objects.exclude(pk=self.instance.pk):
            taken.update(gg.genre_list())

        all_genres = (
            Track.objects.exclude(genre="")
            .values_list("genre", flat=True)
            .distinct()
            .order_by("genre")
        )
        own = set(self.instance.genre_list()) if self.instance.pk else set()
        available = [g for g in all_genres if g not in taken or g in own]
        self.fields["genre_choices"].choices = [(g, g) for g in available]
        if self.instance.pk:
            self.fields["genre_choices"].initial = list(own)

    def save(self, commit=True):
        self.instance.genres = ", ".join(self.cleaned_data["genre_choices"])
        return super().save(commit=commit)


COVER_NAMES = ("cover.jpg", "cover.png", "folder.jpg", "folder.png")


def _find_cover(album):
    """Return the cover Path if found, else None."""
    track = album.tracks.first()
    if not track:
        return None
    album_dir = Path(track.file_path).parent
    for name in COVER_NAMES:
        cover = album_dir / name
        if cover.exists():
            return cover
    return None


class AlbumForm(forms.ModelForm):
    cover_upload = forms.ImageField(required=False, label="Upload cover art")

    class Meta:
        model = Album
        fields = "__all__"

    def save(self, commit=True):
        instance = super().save(commit=commit)
        uploaded = self.cleaned_data.get("cover_upload")
        if uploaded and instance.pk:
            track = instance.tracks.first()
            if track:
                dest = Path(track.file_path).parent / "cover.jpg"
                with open(dest, "wb") as f:
                    for chunk in uploaded.chunks():
                        f.write(chunk)
        return instance


@admin.register(Artist)
class ArtistAdmin(admin.ModelAdmin):
    list_display = ["name", "sort_name"]
    search_fields = ["name"]


@admin.register(Album)
class AlbumAdmin(admin.ModelAdmin):
    form = AlbumForm
    list_display = ["title", "artist", "year", "total_tracks"]
    list_filter = ["year"]
    search_fields = ["title", "artist__name"]
    readonly_fields = ["cover_art"]

    def get_fields(self, request, obj=None):
        fields = super().get_fields(request, obj)
        if obj and _find_cover(obj):
            fields = [f for f in fields if f != "cover_upload"]
        return fields

    @admin.display(description="Cover Art")
    def cover_art(self, obj):
        if not obj.pk:
            return ""
        if _find_cover(obj):
            from django.urls import reverse
            url = reverse("library:cover_art", args=[obj.pk])
            return format_html('<img src="{}" style="max-width:300px; max-height:300px;">', url)
        return "No cover found — use the upload field below."


@admin.register(Track)
class TrackAdmin(admin.ModelAdmin):
    list_display = ["title", "artist", "album", "track_number", "genre", "format", "duration"]
    list_filter = ["format", "genre"]
    search_fields = ["title", "artist__name", "album__title"]


@admin.register(GenreGroup)
class GenreGroupAdmin(admin.ModelAdmin):
    form = GenreGroupForm
    list_display = ["name", "genres", "track_count"]
    search_fields = ["name", "genres"]

    @admin.display(description="Tracks")
    def track_count(self, obj):
        genres = obj.genre_list()
        if not genres:
            return "0 (0%)"
        total = Track.objects.count()
        count = Track.objects.filter(genre__in=genres).count()
        pct = (count / total * 100) if total else 0
        return f"{count} ({pct:.1f}%)"


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
