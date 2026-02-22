from __future__ import annotations

import functools
import json
from io import BytesIO
from pathlib import Path

from django.http import FileResponse, Http404, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from library.models import Album, ApiKey, PlaylistItem


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


@csrf_exempt
@require_api_key
@require_POST
def client_sync(request):
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    # Mark played items
    for entry in body.get("played", []):
        PlaylistItem.objects.filter(pk=entry["id"]).update(played_at=entry["played_at"])

    # Determine items to download
    buffer_bytes = body.get("buffer_cache_mb", 0) * 1024 * 1024
    unplayed = PlaylistItem.objects.filter(played_at__isnull=True).select_related("track").order_by("id")

    unplayed = unplayed.select_related("track__artist", "track__album")

    download = []
    total = 0
    for item in unplayed:
        track = item.track
        size = track.file_size or 0
        if total + size > buffer_bytes and download:
            break
        download.append({
            "id": item.id,
            "title": track.title,
            "artist": track.artist.name,
            "album": track.album.title if track.album else None,
            "album_id": track.album_id,
            "year": track.year,
            "duration": track.duration,
            "file_format": track.format,
        })
        total += size
        if total >= buffer_bytes:
            break

    return JsonResponse({"download": download})


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

    return FileResponse(open(path, "rb"))


MAX_COVER_SIZE = 600


def _resize_cover(image_data):
    """Resize cover art to fit within MAX_COVER_SIZE pixels, return JPEG bytes."""
    from PIL import Image

    img = Image.open(BytesIO(image_data))
    if img.width > MAX_COVER_SIZE or img.height > MAX_COVER_SIZE:
        img.thumbnail((MAX_COVER_SIZE, MAX_COVER_SIZE), Image.LANCZOS)
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=80)
    buf.seek(0)
    return buf


@require_api_key
def cover_art(request, album_id):
    try:
        album = Album.objects.get(pk=album_id)
    except Album.DoesNotExist:
        raise Http404

    # Try file on disk first
    cover_path = _find_cover_file(album)
    if cover_path:
        image_data = cover_path.read_bytes()
        return FileResponse(_resize_cover(image_data), content_type="image/jpeg")

    # Fall back to embedded art
    data, mime = _extract_embedded_art(album)
    if data:
        return FileResponse(_resize_cover(data), content_type="image/jpeg")

    raise Http404
