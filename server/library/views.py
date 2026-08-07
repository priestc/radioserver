from __future__ import annotations

import functools
import json
import mimetypes
import random
import re
from io import BytesIO
from pathlib import Path

from django.http import FileResponse, Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from library.models import Album, ApiKey, Artist, GenreGroup, PlaylistItem, Track


def _file_response(path: Path) -> FileResponse:
    """Return a FileResponse with Content-Length set."""
    response = FileResponse(open(path, "rb"))
    response["Content-Length"] = path.stat().st_size
    return response


def require_api_key(view_func):
    @functools.wraps(view_func)
    def wrapper(request, *args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return JsonResponse({"error": "Authentication required"}, status=401)
        key = auth[7:]
        if not ApiKey.objects.filter(key=key).exists():
            return JsonResponse({"error": "Invalid API key"}, status=401)
        return view_func(request, *args, **kwargs)
    return wrapper

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
COVER_KEYWORDS = ("cover", "front", "folder")


def _find_cover_file(album):
    """Return the cover file Path if found on disk, else None."""
    track = album.tracks.first()
    if not track:
        return None
    album_dir = Path(track.file_path).parent
    if not album_dir.is_dir():
        return None
    for path in album_dir.iterdir():
        if path.suffix.lower() in IMAGE_EXTENSIONS:
            name_lower = path.stem.lower()
            if any(kw in name_lower for kw in COVER_KEYWORDS):
                return path
    return None


def _extract_embedded_art(album):
    """Extract embedded cover art bytes from an album's tracks.

    Returns (image_bytes, mime_type) or (None, None).
    """
    from mutagen import File as MutagenFile

    for track in album.tracks.all():
        try:
            audio = MutagenFile(track.file_path)
        except Exception:
            continue
        if audio is None:
            continue

        # ID3 (MP3) — APIC frames
        if hasattr(audio, "tags") and audio.tags:
            for key in audio.tags:
                if key.startswith("APIC"):
                    apic = audio.tags[key]
                    return apic.data, apic.mime

        # FLAC — pictures list
        if hasattr(audio, "pictures"):
            for pic in audio.pictures:
                return pic.data, pic.mime

        # MP4/M4A — covr atom
        if hasattr(audio, "tags") and audio.tags and "covr" in audio.tags:
            covers = audio.tags["covr"]
            if covers:
                data = bytes(covers[0])
                # MP4Cover format: 13=JPEG, 14=PNG
                fmt = getattr(covers[0], "imageformat", None)
                mime = "image/png" if fmt == 14 else "image/jpeg"
                return data, mime

        # OGG — metadata_block_picture
        if hasattr(audio, "tags") and audio.tags:
            pictures = audio.tags.get("metadata_block_picture")
            if pictures:
                import base64
                from mutagen.flac import Picture
                pic = Picture(base64.b64decode(pictures[0]))
                return pic.data, pic.mime

    return None, None


def has_cover(album):
    """Return True if cover art is available (file or embedded)."""
    if _find_cover_file(album):
        return True
    data, _ = _extract_embedded_art(album)
    return data is not None


def _nuke_cover_art(album):
    """Delete all cover art files on disk and embedded art in audio files."""
    from mutagen import File as MutagenFile

    # Delete cover image files on disk
    track = album.tracks.first()
    if track:
        album_dir = Path(track.file_path).parent
        if album_dir.is_dir():
            for path in album_dir.iterdir():
                if path.suffix.lower() in IMAGE_EXTENSIONS:
                    path.unlink()

    # Strip embedded art from all tracks
    for track in album.tracks.all():
        try:
            audio = MutagenFile(track.file_path)
        except Exception:
            continue
        if audio is None:
            continue

        modified = False

        # ID3 (MP3) — remove APIC frames
        if hasattr(audio, "tags") and audio.tags:
            apic_keys = [k for k in audio.tags if k.startswith("APIC")]
            for key in apic_keys:
                del audio.tags[key]
                modified = True

        # FLAC — clear pictures
        if hasattr(audio, "pictures") and audio.pictures:
            audio.clear_pictures()
            modified = True

        # MP4/M4A — remove covr
        if hasattr(audio, "tags") and audio.tags and "covr" in audio.tags:
            del audio.tags["covr"]
            modified = True

        # OGG — remove metadata_block_picture
        if hasattr(audio, "tags") and audio.tags:
            if "metadata_block_picture" in audio.tags:
                del audio.tags["metadata_block_picture"]
                modified = True

        if modified:
            try:
                audio.save()
            except Exception:
                pass


def check_cover_status(album):
    """Check album cover art and return what was found: 'valid', 'invalid', or ''.

    If invalid art is found, all cover art (files and embedded) is deleted
    and cover_status is set to '' in the database. The return value still
    reflects 'invalid' so callers can report what happened.
    """
    from PIL import Image, UnidentifiedImageError

    cover_path = _find_cover_file(album)
    if cover_path:
        try:
            Image.open(cover_path).verify()
            album.cover_status = Album.COVER_VALID
            album.save(update_fields=["cover_status"])
            return Album.COVER_VALID
        except (UnidentifiedImageError, Exception):
            _nuke_cover_art(album)
            album.cover_status = Album.COVER_NONE
            album.save(update_fields=["cover_status"])
            return Album.COVER_INVALID

    data, _ = _extract_embedded_art(album)
    if data:
        try:
            Image.open(BytesIO(data)).verify()
            album.cover_status = Album.COVER_VALID
            album.save(update_fields=["cover_status"])
            return Album.COVER_VALID
        except (UnidentifiedImageError, Exception):
            _nuke_cover_art(album)
            album.cover_status = Album.COVER_NONE
            album.save(update_fields=["cover_status"])
            return Album.COVER_INVALID

    album.cover_status = Album.COVER_NONE
    album.save(update_fields=["cover_status"])
    return Album.COVER_NONE


@csrf_exempt
@require_api_key
@require_POST
def client_sync(request):
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    # Resolve requested channel (None = default / all music)
    from library.models import Channel
    channel_id = body.get("channel_id")
    channel = None
    if channel_id is not None:
        try:
            channel = Channel.objects.get(pk=channel_id)
        except Channel.DoesNotExist:
            pass

    # Mark played items
    for entry in body.get("played", []):
        PlaylistItem.objects.filter(pk=entry["id"]).update(
            played_at=entry["played_at"],
            skipped=entry.get("skipped", False),
        )

    # Record now-playing start time
    now_playing = body.get("now_playing")
    if now_playing:
        PlaylistItem.objects.filter(pk=now_playing["id"]).update(
            started_at=now_playing["started_at"],
        )

    # Never serve items with an ID at or below the highest ID already played.
    # This prevents stale queued-but-never-reported songs from replaying after
    # higher-ID songs have been played (e.g. after an offline session).
    from django.db.models import Max
    max_played_id = (
        PlaylistItem.objects.filter(played_at__isnull=False, channel=channel)
        .aggregate(max_id=Max("id"))["max_id"]
    ) or 0
    if now_playing:
        max_played_id = max(max_played_id, now_playing["id"])

    # Auto-generate playlist for this channel if unplayed duration is under 1 hour
    from django.db.models import Sum
    unplayed_duration = (
        PlaylistItem.objects.filter(played_at__isnull=True, channel=channel, id__gt=max_played_id)
        .aggregate(total=Sum("track__duration"))["total"]
    ) or 0
    if unplayed_duration < 3600:
        from library.playlist import generate_playlist
        generate_playlist(3600, channel=channel)

    # Determine items to download
    buffer_bytes = body.get("buffer_cache_mb", 0) * 1024 * 1024
    unplayed = PlaylistItem.objects.filter(
        played_at__isnull=True, channel=channel, id__gt=max_played_id
    ).select_related("track").order_by("id")

    unplayed = unplayed.select_related("track__album", "track__album__artist").prefetch_related("track__artists")

    download = []
    total = 0
    for item in unplayed:
        track = item.track
        size = track.file_size or 0
        if total + size > buffer_bytes and download:
            break
        from library.tags import read_replaygain
        rg_gain = read_replaygain(track.file_path)
        download.append({
            "id": item.id,
            "title": track.title,
            "artist": track.display_artist,
            "album": track.album.title if track.album else None,
            "album_id": track.album_id,
            "year": track.year,
            "duration": track.duration,
            "file_format": track.format,
            "replaygain_track_gain": rg_gain,
        })
        total += size
        if total >= buffer_bytes:
            break

    return JsonResponse({"download": download})


@require_api_key
@require_GET
def decade_stations(request, decade_slug):
    from library.models import Decade
    try:
        decade = Decade.objects.prefetch_related("stations__genre_group", "stations__artist").get(name=decade_slug)
    except Decade.DoesNotExist:
        return JsonResponse({"error": f"Decade '{decade_slug}' not found"}, status=404)

    return JsonResponse({
        "decade": decade.name,
        "slug": decade.slug,
        "year_min": decade.year_min,
        "year_max": decade.year_max,
        "stations": [
            {
                "id": s.id,
                "name": s.name,
                "slug": s.slug,
                "genres": s.genre_list(),
                "artist": s.artist.name if s.artist else None,
            }
            for s in decade.stations.all()
        ],
    })


@csrf_exempt
@require_api_key
@require_POST
def decade_station_sync(request, decade_slug, station_slug):
    from library.models import Decade, DecadeStation

    try:
        decade = Decade.objects.get(name=decade_slug)
    except Decade.DoesNotExist:
        return JsonResponse({"error": f"Decade '{decade_slug}' not found"}, status=404)

    try:
        station = DecadeStation.objects.select_related("genre_group", "artist").get(
            decade=decade, slug=station_slug
        )
    except DecadeStation.DoesNotExist:
        return JsonResponse({"error": f"Station '{station_slug}' not found in {decade.name}"}, status=404)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    # Mark played items
    for entry in body.get("played", []):
        PlaylistItem.objects.filter(pk=entry["id"]).update(
            played_at=entry["played_at"],
            skipped=entry.get("skipped", False),
        )

    # Record now-playing start time
    now_playing = body.get("now_playing")
    if now_playing:
        PlaylistItem.objects.filter(pk=now_playing["id"]).update(
            started_at=now_playing["started_at"],
        )

    # Auto-fill queue if unplayed duration is under 1 hour
    from django.db.models import Sum
    unplayed_duration = (
        PlaylistItem.objects.filter(played_at__isnull=True, station=station)
        .aggregate(total=Sum("track__duration"))["total"]
    ) or 0
    if unplayed_duration < 3600:
        _generate_station_playlist(3600, station=station, decade=decade)

    # Return songs to buffer
    buffer_bytes = body.get("buffer_cache_mb", 50) * 1024 * 1024
    unplayed = (
        PlaylistItem.objects.filter(played_at__isnull=True, station=station)
        .select_related("track", "track__album", "track__album__artist")
        .prefetch_related("track__artists")
        .order_by("id")
    )

    download = []
    total = 0
    for item in unplayed:
        track = item.track
        size = track.file_size or 0
        if total + size > buffer_bytes and download:
            break
        from library.tags import read_replaygain
        download.append({
            "id": item.id,
            "title": track.title,
            "artist": track.display_artist,
            "album": track.album.title if track.album else None,
            "album_id": track.album_id,
            "year": track.year,
            "duration": track.duration,
            "file_format": track.format,
            "replaygain_track_gain": read_replaygain(track.file_path),
            "download_url": f"/library/api/download_song/{item.id}/",
        })
        total += size
        if total >= buffer_bytes:
            break

    return JsonResponse({"download": download})


def _generate_station_playlist(target_seconds: float, *, station, decade) -> None:
    """Fill the station's queue with tracks up to target_seconds of audio."""
    import random
    from collections import deque
    from datetime import timedelta

    from django.utils import timezone

    from library.models import GenreGroup, PlaylistSettings, Track

    settings, _ = PlaylistSettings.objects.get_or_create(pk=1)

    qs = Track.objects.filter(exclude_from_playlist=False).exclude(duration__isnull=True)

    # Apply decade year bounds
    qs = qs.filter(year__gte=decade.year_min, year__lte=decade.year_max)

    # Apply station genre filter
    genre_list = station.genre_list()
    if genre_list:
        qs = qs.filter(genre__in=genre_list)

    # Apply station artist filter
    if station.artist:
        qs = qs.filter(artists=station.artist)

    # Exclude already queued and recently played
    already_queued = PlaylistItem.objects.filter(
        played_at__isnull=True, station=station
    ).values_list("track_id", flat=True)
    recently_played = PlaylistItem.objects.filter(
        station=station,
        played_at__gte=timezone.now() - timedelta(days=30),
    ).values_list("track_id", flat=True)
    qs = qs.exclude(id__in=already_queued).exclude(id__in=recently_played)

    tracks = list(qs.select_related("album").prefetch_related("artists"))
    if not tracks:
        return

    for t in tracks:
        t._artist_ids = set(a.id for a in t.artists.all())

    genre_to_group: dict[str, str] = {}
    for gg in GenreGroup.objects.all():
        for g in gg.genre_list():
            genre_to_group[g] = gg.name

    def get_genre_group(track):
        return genre_to_group.get(track.genre)

    recent_artists: deque = deque(maxlen=settings.artist_skip)
    recent_genres: deque = deque(maxlen=settings.genre_skip)
    total_duration = 0.0

    while total_duration < target_seconds:
        for relaxation in range(3):
            candidates = [
                t for t in tracks
                if _station_passes(t, recent_artists, recent_genres, get_genre_group, relaxation)
            ]
            if candidates:
                break
        if not candidates:
            break
        pick = random.choice(candidates)
        PlaylistItem.objects.create(track=pick, station=station)
        total_duration += pick.duration
        recent_artists.append(pick._artist_ids)
        recent_genres.append(get_genre_group(pick))


def _station_passes(track, recent_artists, recent_genres, get_genre_group, relaxation):
    if relaxation < 2:
        if any(track._artist_ids & s for s in recent_artists):
            return False
    if relaxation < 1:
        group = get_genre_group(track)
        if group is not None and group in recent_genres:
            return False
    return True


@require_api_key
@require_GET
def list_channels(request):
    from library.models import Channel
    channels = Channel.objects.select_related("genre_group", "artist").all()
    return JsonResponse({
        "channels": [
            {
                "id": c.id,
                "name": c.name,
                "year_min": c.year_min,
                "year_max": c.year_max,
                "genre_group": c.genre_group.name if c.genre_group else None,
                "genre": c.genre or None,
                "artist": c.artist.name if c.artist else None,
            }
            for c in channels
        ]
    })


@require_api_key
@require_GET
def download_song(request, playlist_item_id):
    try:
        item = PlaylistItem.objects.select_related("track").get(pk=playlist_item_id)
    except PlaylistItem.DoesNotExist:
        raise Http404

    path = Path(item.track.file_path)
    if not path.is_file():
        raise Http404

    return _file_response(path)


@require_api_key
@require_GET
def download_song_lowbitrate(request, playlist_item_id):
    try:
        item = PlaylistItem.objects.select_related("track").get(pk=playlist_item_id)
    except PlaylistItem.DoesNotExist:
        raise Http404

    track = item.track
    path = Path(track.file_path)
    if not path.is_file():
        raise Http404

    # If already 128kbps or lower, serve the original file
    if track.bitrate and track.bitrate <= 128000:
        return _file_response(path)

    # Transcode to 128kbps MP3 via ffmpeg
    import subprocess
    import tempfile

    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp.close()
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(path), "-b:a", "128k", "-map", "a", tmp.name],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        # ffmpeg failed or not installed — fall back to original
        Path(tmp.name).unlink(missing_ok=True)
        return _file_response(path)

    tmp_path = Path(tmp.name)
    fh = open(tmp_path, "rb")
    response = FileResponse(fh, content_type="audio/mpeg")
    response["Content-Length"] = tmp_path.stat().st_size
    response["Content-Disposition"] = f'attachment; filename="{path.stem}.mp3"'
    # Remove temp file once the file handle is closed
    original_close = fh.close
    def _cleanup():
        original_close()
        tmp_path.unlink(missing_ok=True)
    fh.close = _cleanup

    return response


MAX_COVER_SIZE = 600


def _resize_cover(image_data):
    """Resize cover art to fit within MAX_COVER_SIZE pixels, return JPEG bytes.

    Returns None if the image data is not a valid image.
    """
    from PIL import Image, UnidentifiedImageError

    try:
        img = Image.open(BytesIO(image_data))
    except (UnidentifiedImageError, Exception):
        return None
    if img.width > MAX_COVER_SIZE or img.height > MAX_COVER_SIZE:
        img.thumbnail((MAX_COVER_SIZE, MAX_COVER_SIZE), Image.LANCZOS)
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=80)
    buf.seek(0)
    return buf


def cover_art(request, album_id):
    try:
        album = Album.objects.get(pk=album_id)
    except Album.DoesNotExist:
        raise Http404

    # Try file on disk first
    cover_path = _find_cover_file(album)
    if cover_path:
        resized = _resize_cover(cover_path.read_bytes())
        if resized:
            return FileResponse(resized, content_type="image/jpeg")

    # Fall back to embedded art
    data, mime = _extract_embedded_art(album)
    if data:
        resized = _resize_cover(data)
        if resized:
            return FileResponse(resized, content_type="image/jpeg")

    raise Http404


@require_api_key
@require_GET
def list_video_channels(request):
    from library.models import VideoChannel
    channels = VideoChannel.objects.all()
    return JsonResponse({
        "video_channels": [
            {"id": c.id, "name": c.name, "frame_count": c.frame_count, "native_fps": c.native_fps}
            for c in channels
        ]
    })


@require_GET
def video_audio(request, video_channel_id):
    from library.models import VideoChannel
    try:
        channel = VideoChannel.objects.get(pk=video_channel_id)
    except VideoChannel.DoesNotExist:
        raise Http404
    audio_path = channel.get_frame_dir() / "audio.mp3"
    if not audio_path.is_file():
        raise Http404
    return FileResponse(open(audio_path, "rb"), content_type="audio/mpeg")


@require_GET
def video_frame(request, video_channel_id, frame_number):
    from library.models import VideoChannel
    try:
        channel = VideoChannel.objects.get(pk=video_channel_id)
    except VideoChannel.DoesNotExist:
        raise Http404

    if frame_number < 0 or frame_number >= channel.frame_count:
        raise Http404

    frame_path = channel.get_frame_dir() / f"frame_{frame_number:06d}.jpg"
    if not frame_path.is_file():
        raise Http404

    return FileResponse(open(frame_path, "rb"), content_type="image/jpeg")


@csrf_exempt
@require_api_key
@require_POST
def search_tracks(request):
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    filters = body.get("filters", [])
    if not filters:
        return JsonResponse({"tracks": []})

    from django.db.models import Q

    combined_q = Q()
    for filter_set in filters:
        set_q = Q()
        if "genre" in filter_set:
            set_q &= Q(genre__iexact=filter_set["genre"])
        if "artist" in filter_set:
            set_q &= (
                Q(artists__name__iexact=filter_set["artist"])
                | Q(album__artist__name__iexact=filter_set["artist"])
            )
        if "year" in filter_set:
            set_q &= Q(year=filter_set["year"])
        if "year_min" in filter_set:
            set_q &= Q(year__gte=filter_set["year_min"])
        if "year_max" in filter_set:
            set_q &= Q(year__lte=filter_set["year_max"])
        if "duration_min" in filter_set:
            set_q &= Q(duration__gte=filter_set["duration_min"])
        if "duration_max" in filter_set:
            set_q &= Q(duration__lte=filter_set["duration_max"])
        if "album" in filter_set:
            set_q &= Q(album__title__iexact=filter_set["album"])
        if "decade" in filter_set:
            decade_start = int(filter_set["decade"])
            set_q &= Q(year__gte=decade_start, year__lt=decade_start + 10)
        if "genre_group" in filter_set:
            try:
                group = GenreGroup.objects.get(name__iexact=filter_set["genre_group"])
                set_q &= Q(genre__in=group.genre_list())
            except GenreGroup.DoesNotExist:
                set_q &= Q(pk__isnull=True)  # match nothing
        combined_q |= set_q

    all_tracks = list(
        Track.objects.filter(combined_q)
        .filter(exclude_from_playlist=False)
        .exclude(duration__isnull=True)
        .distinct()
        .select_related("album", "album__artist")
        .prefetch_related("artists")
    )
    if not all_tracks:
        return JsonResponse({"tracks": []})

    # Use the same playlist generation logic as the main radio channel
    import random
    from collections import deque
    from library.models import PlaylistSettings

    settings, _ = PlaylistSettings.objects.get_or_create(pk=1)

    for t in all_tracks:
        t._artist_ids = set(a.id for a in t.artists.all())

    genre_to_group: dict[str, str] = {}
    for gg in GenreGroup.objects.all():
        for genre in gg.genre_list():
            genre_to_group[genre] = gg.name

    def get_decade(track):
        year = track.year or (track.album.year if track.album else None)
        return (year // 10 * 10) if year else None

    def get_genre_group(track):
        return genre_to_group.get(track.genre)

    from library.playlist import _passes

    recent_artists: deque[set[int]] = deque(maxlen=settings.artist_skip)
    recent_genres: deque[str | None] = deque(maxlen=settings.genre_skip)
    recent_decades: deque[int | None] = deque(maxlen=settings.decade_skip)

    picked = []
    while len(picked) < 100:
        candidates = None
        for relaxation in range(4):
            candidates = [t for t in all_tracks if t not in picked and _passes(
                t, recent_artists, recent_genres, recent_decades,
                get_genre_group, get_decade, relaxation,
            )]
            if candidates:
                break
        if not candidates:
            break
        pick = random.choice(candidates)
        picked.append(pick)
        recent_artists.append(pick._artist_ids)
        recent_genres.append(get_genre_group(pick))
        recent_decades.append(get_decade(pick))

    from library.tags import read_replaygain

    tracks = []
    for t in picked:
        try:
            rg = read_replaygain(t.file_path)
        except Exception:
            rg = None
        tracks.append({
            "id": t.id,
            "title": t.title,
            "artist": t.display_artist,
            "album": t.album.title if t.album else None,
            "genre": t.genre,
            "year": t.year,
            "duration": t.duration,
            "format": t.format,
            "replaygain_track_gain": rg,
        })

    return JsonResponse({"tracks": tracks})


@require_api_key
@require_GET
def download_track(request, track_id):
    try:
        track = Track.objects.get(pk=track_id)
    except Track.DoesNotExist:
        raise Http404

    path = Path(track.file_path)
    if not path.is_file():
        raise Http404

    return _file_response(path)


AUTOCOMPLETE_DEFAULT_LIMIT = 10
AUTOCOMPLETE_MAX_LIMIT = 25
AUTOCOMPLETE_MIN_QUERY_LENGTH = 2


def _parse_autocomplete_params(request):
    """Parse and validate the shared `q`/`limit` query params.

    Returns (q, limit, error_response); error_response is a JsonResponse
    to return immediately if not None.
    """
    q = request.GET.get("q", "")
    if not isinstance(q, str):
        return None, None, JsonResponse({"error": "'q' must be a string"}, status=400)

    limit = AUTOCOMPLETE_DEFAULT_LIMIT
    limit_param = request.GET.get("limit")
    if limit_param is not None:
        try:
            limit = int(limit_param)
        except ValueError:
            return None, None, JsonResponse({"error": "'limit' must be a positive integer"}, status=400)
        if limit <= 0:
            return None, None, JsonResponse({"error": "'limit' must be a positive integer"}, status=400)
    limit = min(limit, AUTOCOMPLETE_MAX_LIMIT)

    return q.strip(), limit, None


def _ranked_distinct_values(queryset, field, q, limit):
    """Return up to `limit` distinct values of `field`, prefix matches first,
    then alphabetically within each group."""
    from django.db.models import Case, IntegerField, Value, When

    values = (
        queryset
        .annotate(_rank=Case(
            When(**{f"{field}__istartswith": q}, then=Value(0)),
            default=Value(1),
            output_field=IntegerField(),
        ))
        .order_by("_rank", field)
        .values_list(field, flat=True)
    )

    suggestions = []
    seen = set()
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        suggestions.append(value)
        if len(suggestions) >= limit:
            break
    return suggestions


@require_api_key
@require_GET
def autocomplete_artists(request):
    q, limit, error = _parse_autocomplete_params(request)
    if error:
        return error
    if len(q) < AUTOCOMPLETE_MIN_QUERY_LENGTH:
        return JsonResponse({"suggestions": []})

    qs = Artist.objects.filter(name__icontains=q)
    suggestions = _ranked_distinct_values(qs, "name", q, limit)
    return JsonResponse({"suggestions": suggestions})


def _ranked_track_suggestions(queryset, q, limit):
    """Like _ranked_distinct_values, but for the Titles autocomplete: returns
    up to `limit` full track dicts (id/artist/title/year/format/duration —
    see docs/inspiration-server-autocomplete-api.md) instead of bare title
    strings, so the client can add the exact track picked with no secondary
    by-name lookup. Prefix title matches rank first, then alphabetically by
    title; deduplicated by title (case-insensitive) — if more than one track
    genuinely shares a title (e.g. a studio and a live version), either one
    is a fine representative."""
    from django.db.models import Case, IntegerField, Value, When

    tracks = (
        queryset
        .annotate(_rank=Case(
            When(title__istartswith=q, then=Value(0)),
            default=Value(1),
            output_field=IntegerField(),
        ))
        .order_by("_rank", "title")
        .select_related("album", "album__artist")
        .prefetch_related("artists")
    )

    suggestions = []
    seen = set()
    for t in tracks:
        key = t.title.lower()
        if key in seen:
            continue
        seen.add(key)
        suggestions.append({
            "id": t.id,
            "artist": t.display_artist,
            "title": t.title,
            "year": t.year,
            "format": t.format,
            "duration": t.duration,
        })
        if len(suggestions) >= limit:
            break
    return suggestions


@require_api_key
@require_GET
def autocomplete_titles(request):
    q, limit, error = _parse_autocomplete_params(request)
    if error:
        return error
    if len(q) < AUTOCOMPLETE_MIN_QUERY_LENGTH:
        return JsonResponse({"suggestions": []})

    qs = Track.objects.filter(title__icontains=q)

    artist = request.GET.get("artist")
    if artist:
        from django.db.models import Q
        qs = qs.filter(
            Q(artists__name__iexact=artist) | Q(album__artist__name__iexact=artist)
        )

    suggestions = _ranked_track_suggestions(qs, q, limit)
    return JsonResponse({"suggestions": suggestions})


# ---------------------------------------------------------------------------
# Browse UI — unauthenticated, browser-facing album/artist/genre/playback
# ---------------------------------------------------------------------------

def browse_page(request):
    return render(request, "library/browse.html")


def search_landing_page(request):
    return render(request, "library/search.html")


def _search_rank(name, q_lower):
    name_lower = name.lower()
    if name_lower == q_lower:
        return 0
    if name_lower.startswith(q_lower):
        return 1
    return 2


SEARCH_TYPE_PRIORITY = {"artist": 0, "album": 1, "genre": 2, "track": 3}
SEARCH_RESULT_LIMIT = 20


@require_GET
def browse_search(request):
    """Find artists/albums/genres/track-titles matching a free-text query,
    ranked best-match first. The top-level search box redirects straight to
    the destination page when there's exactly one result, and to a search
    results page (listing all of these) when there's more than one."""
    q = request.GET.get("q", "").strip()
    if not q:
        return JsonResponse({"results": []})

    q_lower = q.lower()
    candidates = []

    for artist in Artist.objects.filter(name__icontains=q)[:8]:
        candidates.append((
            _search_rank(artist.name, q_lower), SEARCH_TYPE_PRIORITY["artist"], artist.name.lower(),
            {"type": "artist", "id": artist.id, "label": artist.name, "subtitle": "Artist"},
        ))

    for album in Album.objects.filter(title__icontains=q).select_related("artist")[:8]:
        candidates.append((
            _search_rank(album.title, q_lower), SEARCH_TYPE_PRIORITY["album"], album.title.lower(),
            {
                "type": "album", "id": album.id, "label": album.title,
                "subtitle": "Album by " + album.artist.name,
            },
        ))

    genre_names = (
        Track.objects.filter(genre__icontains=q)
        .exclude(genre="")
        .order_by()
        .values_list("genre", flat=True)
        .distinct()[:8]
    )
    for genre in genre_names:
        candidates.append((
            _search_rank(genre, q_lower), SEARCH_TYPE_PRIORITY["genre"], genre.lower(),
            {"type": "genre", "name": genre, "label": genre, "subtitle": "Genre mix"},
        ))

    for track in Track.objects.filter(title__icontains=q).select_related("album", "album__artist")[:8]:
        if track.album_id:
            subtitle = "Song by " + track.display_artist
            if track.album:
                subtitle += " · " + track.album.title
            candidates.append((
                _search_rank(track.title, q_lower), SEARCH_TYPE_PRIORITY["track"], track.title.lower(),
                {"type": "album", "id": track.album_id, "label": track.title, "subtitle": subtitle},
            ))

    candidates.sort(key=lambda c: (c[0], c[1], c[2]))

    # Dedup by destination (e.g. two matching tracks off the same album would
    # otherwise point at, and list, that album twice) — keep the best-ranked
    # occurrence since candidates is already sorted.
    seen = set()
    results = []
    for _, _, _, payload in candidates:
        key = (payload["type"], payload.get("id", payload.get("name")))
        if key in seen:
            continue
        seen.add(key)
        results.append(payload)
        if len(results) >= SEARCH_RESULT_LIMIT:
            break

    return JsonResponse({"results": results})


@require_GET
def browse_albums(request):
    albums = Album.objects.select_related("artist").order_by(
        "artist__sort_name", "artist__name", "year", "title"
    )
    return JsonResponse({
        "albums": [
            {
                "id": a.id,
                "title": a.title,
                "artist": a.artist.name,
                "artist_id": a.artist_id,
                "year": a.year,
            }
            for a in albums
        ]
    })


@require_GET
def browse_album_detail(request, album_id):
    try:
        album = Album.objects.select_related("artist").get(pk=album_id)
    except Album.DoesNotExist:
        raise Http404

    tracks = album.tracks.prefetch_related("artists")
    return JsonResponse({
        "id": album.id,
        "title": album.title,
        "artist": album.artist.name,
        "artist_id": album.artist_id,
        "year": album.year,
        "tracks": [
            {
                "id": t.id,
                "title": t.title,
                "track_number": t.track_number,
                "disc_number": t.disc_number,
                "duration": t.duration,
                "artist": t.display_artist,
            }
            for t in tracks
        ],
    })


@require_GET
def browse_artists(request):
    from django.db.models import Count

    artists = (
        Artist.objects.annotate(album_count=Count("albums", distinct=True))
        .filter(album_count__gt=0)
        .order_by("sort_name", "name")
    )
    return JsonResponse({
        "artists": [
            {"id": ar.id, "name": ar.name, "album_count": ar.album_count}
            for ar in artists
        ]
    })


@require_GET
def browse_artist_detail(request, artist_id):
    try:
        artist = Artist.objects.get(pk=artist_id)
    except Artist.DoesNotExist:
        raise Http404

    albums = artist.albums.order_by("year", "title")
    return JsonResponse({
        "id": artist.id,
        "name": artist.name,
        "albums": [
            {"id": al.id, "title": al.title, "year": al.year}
            for al in albums
        ],
    })


GENRE_COLLAGE_POOL = 6
GENRE_MIN_ALBUMS = 3


@require_GET
def browse_genres(request):
    from django.db.models import Count

    genres = (
        Track.objects.exclude(genre="")
        .values("genre")
        .annotate(count=Count("id"))
        .order_by("genre")
    )

    album_ids_by_genre = {}
    genre_album_pairs = (
        Track.objects.exclude(genre="")
        .filter(album__isnull=False)
        # Track's default Meta.ordering rides along into this query's ORDER
        # BY, which breaks .distinct() on just (genre, album_id) — clear it.
        .order_by()
        .values_list("genre", "album_id")
        .distinct()
    )
    for genre_name, album_id in genre_album_pairs:
        album_ids_by_genre.setdefault(genre_name, []).append(album_id)

    result = []
    for g in genres:
        pool = album_ids_by_genre.get(g["genre"], [])
        # Genres backed by only a couple of albums are usually mis-tagged
        # one-off tracks rather than a real genre, and can't make a decent
        # collage anyway — leave them off the genre browse page.
        if len(pool) < GENRE_MIN_ALBUMS:
            continue
        result.append({
            "name": g["genre"],
            "count": g["count"],
            # Random sample rather than a deterministic low-id-first pick —
            # otherwise large genres consistently show the same handful of
            # (often art-less, bulk-imported) low-id albums.
            "album_ids": random.sample(pool, min(len(pool), GENRE_COLLAGE_POOL)),
        })

    return JsonResponse({"genres": result})


GENRE_MIX_LIMIT = 100


@require_GET
def browse_genre_mix(request, genre):
    tracks = list(
        Track.objects.filter(genre__iexact=genre, exclude_from_playlist=False)
        .select_related("album", "album__artist")
        .prefetch_related("artists")
    )
    random.shuffle(tracks)
    tracks = tracks[:GENRE_MIX_LIMIT]
    return JsonResponse({
        "genre": genre,
        "tracks": [
            {
                "id": t.id,
                "title": t.title,
                "artist": t.display_artist,
                "year": t.year,
                "album": t.album.title if t.album else None,
                "album_id": t.album_id,
                "duration": t.duration,
            }
            for t in tracks
        ],
    })


RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
STREAM_CHUNK_SIZE = 8192


def _ranged_file_stream(path, start, length):
    with open(path, "rb") as f:
        f.seek(start)
        remaining = length
        while remaining > 0:
            chunk = f.read(min(STREAM_CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@require_GET
def browse_stream_track(request, track_id):
    """Stream a track for inline browser playback, honoring Range requests
    (needed for seeking in an <audio> element) — Django's FileResponse does
    not implement Range support itself, so it's handled manually here."""
    try:
        track = Track.objects.get(pk=track_id)
    except Track.DoesNotExist:
        raise Http404

    path = Path(track.file_path)
    if not path.is_file():
        raise Http404

    file_size = path.stat().st_size
    content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"

    range_header = request.headers.get("Range", "")
    range_match = RANGE_RE.match(range_header) if range_header else None

    if range_match:
        start_str, end_str = range_match.groups()
        start = int(start_str) if start_str else 0
        end = int(end_str) if end_str else file_size - 1
        end = min(end, file_size - 1)
        if start >= file_size or start > end:
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{file_size}"
            return response

        length = end - start + 1
        response = StreamingHttpResponse(
            _ranged_file_stream(path, start, length),
            status=206,
            content_type=content_type,
        )
        response["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        response["Content-Length"] = str(length)
    else:
        response = FileResponse(open(path, "rb"), content_type=content_type)
        response["Content-Length"] = str(file_size)

    response["Accept-Ranges"] = "bytes"
    response["Content-Disposition"] = "inline"
    response["Cache-Control"] = "no-cache"
    return response
