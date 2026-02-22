from __future__ import annotations

from io import BytesIO
from pathlib import Path

from django.http import FileResponse, Http404
from django.contrib.admin.views.decorators import staff_member_required

from library.models import Album

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


@staff_member_required
def cover_art(request, album_id):
    try:
        album = Album.objects.get(pk=album_id)
    except Album.DoesNotExist:
        raise Http404

    # Try file on disk first
    cover_path = _find_cover_file(album)
    if cover_path:
        return FileResponse(open(cover_path, "rb"))

    # Fall back to embedded art
    data, mime = _extract_embedded_art(album)
    if data:
        return FileResponse(BytesIO(data), content_type=mime)

    raise Http404
