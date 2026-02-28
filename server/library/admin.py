from pathlib import Path
from urllib.parse import quote_plus

from django import forms
from django.conf import settings
from django.contrib import admin
from django.http import JsonResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from library.models import (
    AIServiceError,
    AIServiceManager,
    Album,
    ApiKey,
    Artist,
    GenreGroup,
    PlaylistItem,
    PlaylistSettings,
    Track,
    YtdlDownload,
)
from library.views import has_cover, _nuke_cover_art


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
            instance.cover_status = Album.COVER_VALID
            instance.save(update_fields=["cover_status"])
        return instance


@admin.register(Artist)
class ArtistAdmin(admin.ModelAdmin):
    list_display = ["display_name", "sort_name", "exclude_from_playlist"]
    list_editable = ["exclude_from_playlist"]
    search_fields = ["name"]
    readonly_fields = ["album_list", "track_list"]

    @admin.display(description="Name")
    def display_name(self, obj):
        if obj.exclude_from_playlist:
            return format_html('<span style="opacity:0.35">{}</span>', obj.name)
        return obj.name

    @admin.display(description="Albums")
    def album_list(self, obj):
        if not obj.pk:
            return ""
        from django.urls import reverse
        albums = obj.albums.order_by("year", "title")
        if not albums.exists():
            return "No albums"
        items = []
        for a in albums:
            url = reverse("admin:library_album_change", args=[a.pk])
            label = f"{a.title} ({a.year})" if a.year else a.title
            items.append(format_html('<li><a href="{}">{}</a></li>', url, label))
        return format_html("<ul style='margin:0;padding-left:1.5em'>{}</ul>", format_html("".join(items)))

    @admin.display(description="Tracks")
    def track_list(self, obj):
        if not obj.pk:
            return ""
        from django.urls import reverse
        tracks = obj.tracks.order_by("title")
        if not tracks.exists():
            return "No tracks"
        items = []
        for t in tracks:
            url = reverse("admin:library_track_change", args=[t.pk])
            album_label = f" ({t.album.title})" if t.album else ""
            items.append(format_html('<li><a href="{}">{}</a>{}</li>', url, t.title, album_label))
        return format_html("<ol style='margin:0;padding-left:2.5em'>{}</ol>", format_html("".join(items)))

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if change and "exclude_from_playlist" in form.changed_data:
            obj.albums.update(exclude_from_playlist=obj.exclude_from_playlist)
            Track.objects.filter(artists=obj).update(exclude_from_playlist=obj.exclude_from_playlist)


@admin.register(Album)
class AlbumAdmin(admin.ModelAdmin):
    form = AlbumForm
    list_display = ["display_title", "artist", "year", "total_tracks", "has_artwork", "exclude_from_playlist"]
    list_editable = ["exclude_from_playlist"]
    list_filter = ["year", "cover_status"]
    search_fields = ["title", "artist__name"]
    readonly_fields = ["cover_art", "track_list", "strip_track_years_btn", "ai_date_finder_btn"]

    def get_fields(self, request, obj=None):
        fields = super().get_fields(request, obj)
        if obj and has_cover(obj):
            fields = [f for f in fields if f != "cover_upload"]
        return fields

    @admin.display(description="Cover Art")
    def cover_art(self, obj):
        if not obj.pk:
            return ""
        if has_cover(obj):
            from django.urls import reverse
            url = reverse("library:cover_art", args=[obj.pk])
            return format_html('<img src="{}" style="max-width:300px; max-height:300px;">', url)
        query = quote_plus(f"{obj.artist.name} {obj.title} album cover")
        search_url = f"https://www.google.com/search?tbm=isch&tbs=isz:l&q={query}"
        return format_html(
            '<a href="{}" target="_blank">Search Google Images</a>'
            " — or use the upload field below.",
            search_url,
        )

    @admin.display(description="Artwork", boolean=True)
    def has_artwork(self, obj):
        if obj.cover_status == "invalid":
            return False
        if obj.cover_status == "valid":
            return True
        return None

    @admin.display(description="Tracks")
    def track_list(self, obj):
        if not obj.pk:
            return ""
        tracks = obj.tracks.order_by("disc_number", "track_number", "title")
        if not tracks.exists():
            return "No tracks"
        multi_disc = obj.total_discs and obj.total_discs > 1
        from django.urls import reverse
        items = []
        for t in tracks:
            prefix = f"{t.disc_number}-" if t.disc_number and multi_disc else ""
            num = f"{prefix}{t.track_number}. " if t.track_number else ""
            url = reverse("admin:library_track_change", args=[t.pk])
            year_str = f" ({t.year})" if t.year else ""
            items.append(format_html('<li>{}<a href="{}">{}</a>{}</li>', num, url, t.title, year_str))
        return format_html("<ol style='margin:0;padding-left:1.5em'>{}</ol>", format_html("".join(items)))

    @admin.display(description="Title")
    def display_title(self, obj):
        greyed = obj.exclude_from_playlist or obj.artist.exclude_from_playlist
        if greyed:
            return format_html('<span style="opacity:0.35">{}</span>', obj.title)
        return obj.title

    @admin.display(description="Strip Track Years")
    def strip_track_years_btn(self, obj):
        if not obj.pk:
            return ""
        return format_html(
            '<button type="button" class="button" onclick="stripTrackYears({pk})">'
            'Strip track years</button>'
            ' <span id="strip-years-result"></span>'
            '<script>'
            'function stripTrackYears(pk) {{'
            '  if (!confirm("Remove the year from all tracks in this album?")) return;'
            '  var result = document.getElementById("strip-years-result");'
            '  result.textContent = "Stripping...";'
            '  result.style.color = "";'
            '  fetch("/admin/library/album/" + pk + "/strip-track-years/", {{'
            '    method: "POST",'
            '    headers: {{"X-CSRFToken": document.querySelector("[name=csrfmiddlewaretoken]").value}}'
            '  }})'
            '  .then(function(r) {{ return r.json(); }})'
            '  .then(function(data) {{'
            '    if (data.ok) {{'
            '      result.textContent = "Cleared year from " + data.count + " track(s).";'
            '      result.style.color = "#2e7d32";'
            '    }} else {{'
            '      result.textContent = "Error: " + data.error;'
            '      result.style.color = "red";'
            '    }}'
            '  }})'
            '  .catch(function(err) {{'
            '    result.textContent = "Request failed: " + err;'
            '    result.style.color = "red";'
            '  }});'
            '}}'
            '</script>',
            pk=obj.pk,
        )

    @admin.display(description="AI Date Finder")
    def ai_date_finder_btn(self, obj):
        if not obj.pk:
            return ""
        from django.urls import reverse
        url = reverse("admin:library_album_ai_date_finder", args=[obj.pk])
        return format_html(
            '<a href="{}" class="button">AI Date Finder</a>',
            url,
        )

    actions = ["delete_cover_art", "ai_date_finder"]

    def get_urls(self):
        custom_urls = [
            path(
                "<int:album_id>/ai-date-finder/",
                self.admin_site.admin_view(self.ai_date_finder_view),
                name="library_album_ai_date_finder",
            ),
            path(
                "<int:album_id>/ai-date-finder/lookup/<int:track_id>/",
                self.admin_site.admin_view(self.ai_date_finder_lookup),
                name="library_album_ai_date_finder_lookup",
            ),
            path(
                "<int:album_id>/strip-track-years/",
                self.admin_site.admin_view(self.strip_track_years_view),
                name="library_album_strip_track_years",
            ),
        ]
        return custom_urls + super().get_urls()

    @admin.action(description="AI Date Finder — look up track years")
    def ai_date_finder(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, "Please select exactly one album.", level="error")
            return
        album = queryset.first()
        return redirect("admin:library_album_ai_date_finder", album_id=album.pk)

    def ai_date_finder_view(self, request, album_id):
        from library.ai import get_available_backends

        album = Album.objects.get(id=album_id)
        available = get_available_backends()

        if not available:
            return TemplateResponse(request, "admin/library/ai_date_finder.html", {
                "album": album,
                "backend": "none",
                "error": "No AI backends configured. Add an API key via radioserver install_* commands.",
                "tracks": [],
            })

        backend_name = request.GET.get("backend") or request.POST.get("backend") or available[0]
        if backend_name not in available:
            backend_name = available[0]

        if request.method == "POST" and request.POST.get("confirm"):
            from library.tags import write_track_year

            tracks = album.tracks.all()
            updated = 0
            for track in tracks:
                raw = request.POST.get(f"year_{track.pk}", "").strip()
                if raw:
                    try:
                        track.year = int(raw)
                        track.save(update_fields=["year"])
                        write_track_year(track)
                        updated += 1
                    except (ValueError, TypeError):
                        pass
            self.message_user(request, f"Updated {updated} track(s).")
            return redirect("admin:library_album_change", album_id)

        # Build track metadata without querying AI
        track_data = []
        for track in album.tracks.order_by("disc_number", "track_number", "title"):
            artist = track.artists.first()
            artist_name = artist.name if artist else "Unknown Artist"
            track_data.append({
                "track_id": track.pk,
                "title": track.title,
                "artist": artist_name,
                "current_year": track.year,
            })

        return TemplateResponse(request, "admin/library/ai_date_finder.html", {
            "album": album,
            "backend": backend_name,
            "tracks": track_data,
        })

    def ai_date_finder_lookup(self, request, album_id, track_id):
        from library.ai import lookup_year_with_fallback

        backend_name = request.GET.get("backend", "")
        track = Track.objects.get(pk=track_id, album_id=album_id)
        artist = track.artists.first()
        artist_name = artist.name if artist else "Unknown Artist"

        year, used_backend, errors = lookup_year_with_fallback(
            track.title, artist_name, preferred_backend=backend_name,
        )
        if year is not None:
            result = {"year": year, "backend": used_backend}
            if errors:
                result["warnings"] = errors
            return JsonResponse(result)
        return JsonResponse({"error": "; ".join(errors) or "Could not parse year"})

    def strip_track_years_view(self, request, album_id):
        if request.method != "POST":
            return JsonResponse({"error": "POST required"}, status=405)
        album = Album.objects.get(pk=album_id)
        count = album.tracks.exclude(year=None).update(year=None)
        return JsonResponse({"ok": True, "count": count})

    @admin.action(description="Delete cover art")
    def delete_cover_art(self, request, queryset):
        count = 0
        for album in queryset:
            if has_cover(album):
                _nuke_cover_art(album)
                album.cover_status = Album.COVER_NONE
                album.save(update_fields=["cover_status"])
                count += 1
        self.message_user(request, f"Deleted cover art from {count} album(s).")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if change and "exclude_from_playlist" in form.changed_data:
            obj.tracks.update(exclude_from_playlist=obj.exclude_from_playlist)
        from library.tags import write_album_tags
        write_album_tags(obj)


class TrackForm(forms.ModelForm):
    track_artists = forms.CharField(
        required=False,
        label="Artists",
        help_text="Comma-separated list of artist names.",
        widget=forms.TextInput(attrs={"style": "width: 400px;"}),
    )

    class Meta:
        model = Track
        exclude = ["artists"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            names = list(
                self.instance.artists
                .order_by("trackartist__position")
                .values_list("name", flat=True)
            )
            self.fields["track_artists"].initial = ", ".join(names)

    def save(self, commit=True):
        instance = super().save(commit=commit)
        if instance.pk:
            from library.models import TrackArtist
            raw = self.cleaned_data.get("track_artists", "")
            names = [n.strip() for n in raw.split(",") if n.strip()]
            TrackArtist.objects.filter(track=instance).delete()
            for i, name in enumerate(names):
                artist, _ = Artist.objects.get_or_create(name=name)
                TrackArtist.objects.create(track=instance, artist=artist, position=i)

            from library.tags import write_track_tags
            write_track_tags(instance)
        return instance


@admin.register(Track)
class TrackAdmin(admin.ModelAdmin):
    form = TrackForm
    list_display = ["display_title", "display_artist_name", "album", "track_number", "genre", "format", "duration", "replaygain", "exclude_from_playlist"]
    list_editable = ["exclude_from_playlist"]
    list_filter = ["format", "genre", "source"]
    search_fields = ["title", "artists__name", "album__title", "source"]
    readonly_fields = ["audio_player", "ai_year_lookup"]

    def get_urls(self):
        custom_urls = [
            path(
                "<int:track_id>/ai-year-lookup/",
                self.admin_site.admin_view(self.ai_year_lookup_view),
                name="library_track_ai_year_lookup",
            ),
            path(
                "<int:track_id>/stream/",
                self.admin_site.admin_view(self.stream_view),
                name="library_track_stream",
            ),
        ]
        return custom_urls + super().get_urls()

    def stream_view(self, request, track_id):
        from django.http import FileResponse
        track = Track.objects.get(pk=track_id)
        file_path = Path(track.file_path)
        if not file_path.is_file():
            return JsonResponse({"error": "File not found"}, status=404)
        return FileResponse(open(file_path, "rb"))

    @admin.display(description="Player")
    def audio_player(self, obj):
        if not obj.pk:
            return ""
        from django.urls import reverse
        url = reverse("admin:library_track_stream", args=[obj.pk])
        return format_html(
            '<audio controls preload="none" style="width:100%; max-width:500px;">'
            '<source src="{}">'
            '</audio>',
            url,
        )

    def ai_year_lookup_view(self, request, track_id):
        from library.ai import lookup_year_with_fallback

        track = Track.objects.get(pk=track_id)
        artist = track.artists.first()
        artist_name = artist.name if artist else "Unknown Artist"

        year, used_backend, errors = lookup_year_with_fallback(
            track.title, artist_name,
        )
        if year is not None:
            result = {"year": year, "backend": used_backend}
            if errors:
                result["warnings"] = errors
            return JsonResponse(result)
        return JsonResponse({"error": "; ".join(errors) or "Could not parse year"})

    @admin.display(description="AI Year Lookup")
    def ai_year_lookup(self, obj):
        if not obj.pk:
            return ""
        return format_html(
            '<button type="button" class="button" onclick="aiYearLookup({pk})">'
            'Look up year</button>'
            ' <span id="ai-year-spinner" style="display:none">Looking up...</span>'
            ' <span id="ai-year-result"></span>'
            '<script>'
            'function aiYearLookup(pk) {{'
            '  var spinner = document.getElementById("ai-year-spinner");'
            '  var result = document.getElementById("ai-year-result");'
            '  spinner.style.display = "inline";'
            '  result.textContent = "";'
            '  result.style.color = "";'
            '  fetch("/admin/library/track/" + pk + "/ai-year-lookup/")'
            '    .then(function(r) {{ return r.json(); }})'
            '    .then(function(data) {{'
            '      spinner.style.display = "none";'
            '      if (data.error) {{'
            '        result.textContent = data.error;'
            '        result.style.color = "red";'
            '      }} else {{'
            '        var msg = data.year;'
            '        if (data.backend) msg += " (via " + data.backend + ")";'
            '        result.textContent = msg;'
            '        result.style.color = "#2e7d32";'
            '      }}'
            '    }})'
            '    .catch(function(err) {{'
            '      spinner.style.display = "none";'
            '      result.textContent = "Request failed: " + err;'
            '      result.style.color = "red";'
            '    }});'
            '}}'
            '</script>',
            pk=obj.pk,
        )

    @admin.display(description="Artist")
    def display_artist_name(self, obj):
        return obj.display_artist

    @admin.display(description="RG")
    def replaygain(self, obj):
        from library.tags import read_replaygain
        gain = read_replaygain(obj.file_path)
        if gain is None:
            return ""
        return f"{gain:+.1f} dB"

    @admin.display(description="Title")
    def display_title(self, obj):
        greyed = (
            obj.exclude_from_playlist
            or obj.artists.filter(exclude_from_playlist=True).exists()
            or (obj.album and obj.album.exclude_from_playlist)
        )
        if greyed:
            return format_html('<span style="opacity:0.35">{}</span>', obj.title)
        return obj.title


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


@admin.register(PlaylistItem)
class PlaylistItemAdmin(admin.ModelAdmin):
    list_display = ["id", "track", "duration", "played_at"]
    ordering = ["-id"]
    raw_id_fields = ["track"]

    @admin.display(description="Duration")
    def duration(self, obj):
        secs = obj.track.duration
        if secs is None:
            return ""
        mins, secs = divmod(int(secs), 60)
        return f"{mins}:{secs:02d}"


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ["key", "label", "created_at"]
    readonly_fields = ["key", "created_at", "qr_code"]
    fields = ["key", "qr_code", "label", "created_at"]

    @admin.display(description="QR Code")
    def qr_code(self, obj):
        if not obj.pk:
            return ""
        from library.qr import make_qr_svg
        svg = make_qr_svg(obj.key)
        return mark_safe(f'<div style="max-width:200px">{svg}</div>')


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


@admin.register(AIServiceManager)
class AIServiceManagerAdmin(admin.ModelAdmin):
    list_display = ["display_name"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        custom_urls = [
            path(
                "<int:pk>/set-key/",
                self.admin_site.admin_view(self.set_key_view),
                name="library_aiservicemanager_set_key",
            ),
            path(
                "<int:pk>/test/",
                self.admin_site.admin_view(self.test_view),
                name="library_aiservicemanager_test",
            ),
            path(
                "<int:pk>/toggle-enabled/",
                self.admin_site.admin_view(self.toggle_enabled_view),
                name="library_aiservicemanager_toggle_enabled",
            ),
        ]
        return custom_urls + super().get_urls()

    def changelist_view(self, request, extra_context=None):
        from datetime import timedelta

        from django.utils import timezone as tz

        from library.ai import CONF_KEYS, ensure_services

        ensure_services()
        cutoff = tz.now() - timedelta(minutes=5)

        services = []
        for svc in AIServiceManager.objects.all():
            conf_key, settings_attr = CONF_KEYS.get(svc.name, (None, None))
            key_configured = bool(settings_attr and getattr(settings, settings_attr, ""))
            recent_errors = svc.errors.count()
            cooloff = svc.errors.filter(created_at__gte=cutoff).exists()
            services.append({
                "pk": svc.pk,
                "name": svc.name,
                "display_name": svc.display_name,
                "key_configured": key_configured,
                "recent_errors": recent_errors,
                "cooloff": cooloff,
                "enabled": svc.enabled,
            })

        context = {
            **self.admin_site.each_context(request),
            "title": "AI Services",
            "services": services,
            "opts": self.model._meta,
            "has_add_permission": False,
        }
        return TemplateResponse(
            request,
            "admin/library/aiservicemanager/change_list.html",
            context,
        )

    def set_key_view(self, request, pk):
        from library.ai import save_api_key

        if request.method != "POST":
            return JsonResponse({"error": "POST required"}, status=405)
        svc = AIServiceManager.objects.get(pk=pk)
        key = request.POST.get("api_key", "").strip()
        if not key:
            return JsonResponse({"error": "No key provided"}, status=400)
        try:
            save_api_key(svc.name, key)
            return JsonResponse({"ok": True})
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=400)

    def test_view(self, request, pk):
        from library.ai import test_backend

        svc = AIServiceManager.objects.get(pk=pk)
        success, message = test_backend(svc.name)
        return JsonResponse({"success": success, "message": message})

    def toggle_enabled_view(self, request, pk):
        if request.method != "POST":
            return JsonResponse({"error": "POST required"}, status=405)
        svc = AIServiceManager.objects.get(pk=pk)
        svc.enabled = not svc.enabled
        svc.save(update_fields=["enabled"])
        return JsonResponse({"ok": True, "enabled": svc.enabled})


@admin.register(YtdlDownload)
class YtdlDownloadAdmin(admin.ModelAdmin):
    list_display = ["artist_name", "album_title", "status", "created_at"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        custom_urls = [
            path(
                "fetch-metadata/",
                self.admin_site.admin_view(self.fetch_metadata_view),
                name="library_ytdldownload_fetch_metadata",
            ),
            path(
                "start-download/",
                self.admin_site.admin_view(self.start_download_view),
                name="library_ytdldownload_start_download",
            ),
            path(
                "download-status/<int:pk>/",
                self.admin_site.admin_view(self.download_status_view),
                name="library_ytdldownload_download_status",
            ),
        ]
        return custom_urls + super().get_urls()

    def changelist_view(self, request, extra_context=None):
        downloads = YtdlDownload.objects.all()[:50]
        context = {
            **self.admin_site.each_context(request),
            "title": "YouTube Downloads",
            "downloads": downloads,
            "opts": self.model._meta,
            "has_add_permission": False,
        }
        return TemplateResponse(
            request,
            "admin/library/ytdldownload/change_list.html",
            context,
        )

    def fetch_metadata_view(self, request):
        if request.method != "POST":
            return JsonResponse({"error": "POST required"}, status=405)
        url = request.POST.get("url", "").strip()
        if not url:
            return JsonResponse({"error": "No URL provided"}, status=400)
        try:
            from library.ytdl import get_metadata_from_ytdl
            metadata = get_metadata_from_ytdl(url)
            return JsonResponse(metadata)
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=400)

    def start_download_view(self, request):
        import json as json_mod
        import threading

        if request.method != "POST":
            return JsonResponse({"error": "POST required"}, status=405)

        data = json_mod.loads(request.body)
        url = data.get("url", "").strip()
        artist_name = data.get("artist_name", "").strip()
        album_title = data.get("album_title", "").strip()
        track_overrides = data.get("tracks", [])
        use_track_albums = bool(data.get("use_track_albums", False))

        if not url:
            return JsonResponse({"error": "No URL provided"}, status=400)

        # Check for duplicate
        from library.models import Album
        if album_title and artist_name and not use_track_albums:
            if Album.objects.filter(
                title__iexact=album_title, artist__name__iexact=artist_name,
            ).exists():
                return JsonResponse(
                    {"error": f"Album already in library: {artist_name} — {album_title}"},
                    status=400,
                )

        dl = YtdlDownload.objects.create(
            url=url,
            artist_name=artist_name,
            album_title=album_title,
            track_overrides=track_overrides,
            use_track_albums=use_track_albums,
            status="pending",
        )

        from library.ytdl import run_download
        thread = threading.Thread(daemon=True, target=run_download, args=(dl.pk,))
        thread.start()

        return JsonResponse({"id": dl.pk})

    def download_status_view(self, request, pk):
        try:
            dl = YtdlDownload.objects.get(pk=pk)
        except YtdlDownload.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)
        data = {
            "status": dl.status,
            "progress_message": dl.progress_message,
            "error_message": dl.error_message,
            "album_id": dl.album_id,
        }
        return JsonResponse(data)


@admin.register(AIServiceError)
class AIServiceErrorAdmin(admin.ModelAdmin):
    list_display = ["service", "short_message", "created_at"]
    list_filter = ["service"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="Error message")
    def short_message(self, obj):
        msg = obj.error_message
        if len(msg) > 120:
            return msg[:120] + "..."
        return msg
