from __future__ import annotations

import functools
import json
from io import BytesIO
from pathlib import Path

from django.http import FileResponse, Http404, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from library.models import Album, ApiKey, GenreGroup, PlaylistItem, Track


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

    # If the client has switched channels, discard unplayed items from the old channel
    first_unplayed = PlaylistItem.objects.filter(played_at__isnull=True).order_by("id").first()
    if first_unplayed is not None and first_unplayed.channel != channel:
        PlaylistItem.objects.filter(played_at__isnull=True).delete()

    # Auto-generate playlist for this channel if unplayed duration is under 1 hour
    from django.db.models import Sum
    unplayed_duration = (
        PlaylistItem.objects.filter(played_at__isnull=True, channel=channel)
        .aggregate(total=Sum("track__duration"))["total"]
    ) or 0
    if unplayed_duration < 3600:
        from library.playlist import generate_playlist
        generate_playlist(3600, channel=channel)

    # Determine items to download
    buffer_bytes = body.get("buffer_cache_mb", 0) * 1024 * 1024
    unplayed = PlaylistItem.objects.filter(played_at__isnull=True, channel=channel).select_related("track").order_by("id")

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
