from __future__ import annotations

import os
import time
from pathlib import Path

from django.conf import settings

from library.models import Album, Artist, Track
from library.tags import read_tags
from library.views import check_cover_status


def _get_or_create_artist(name: str) -> Artist:
    artist, _ = Artist.objects.get_or_create(name=name)
    return artist


def _get_or_create_album(title: str, artist: Artist, tag_data: dict) -> Album:
    album, created = Album.objects.get_or_create(
        title=title,
        artist=artist,
    )
    if created or album.year is None:
        album.year = tag_data.get("year")
        album.total_tracks = tag_data.get("total_tracks")
        album.total_discs = tag_data.get("total_discs")
        album.save()
    return album


def _upsert_track(tag_data: dict, source: str = "") -> tuple[bool, list[str]]:
    """Create or update a Track from tag_data.

    Returns (created, changed_fields) where changed_fields lists field names
    that differ from the existing record. Empty list if created.
    """
    artist = _get_or_create_artist(tag_data["artist"])
    album_artist = _get_or_create_artist(tag_data["album_artist"])
    album = _get_or_create_album(tag_data["album"], album_artist, tag_data)

    defaults = {
        "title": tag_data["title"],
        "artist": artist,
        "album": album,
        "album_artist": album_artist,
        "track_number": tag_data["track_number"],
        "disc_number": tag_data["disc_number"],
        "genre": tag_data["genre"],
        "year": tag_data["year"],
        "duration": tag_data["duration"],
        "bitrate": tag_data["bitrate"],
        "sample_rate": tag_data["sample_rate"],
        "channels": tag_data["channels"],
        "format": tag_data["format"],
        "file_size": tag_data["file_size"],
        "file_mtime": tag_data["file_mtime"],
    }

    # Check what changed before upserting
    changed_fields = []
    try:
        existing = Track.objects.get(file_path=tag_data["file_path"])
        for field, new_val in defaults.items():
            old_val = getattr(existing, field)
            if field in ("artist", "album", "album_artist"):
                old_val = getattr(existing, f"{field}_id")
                new_val = new_val.pk if new_val else None
            if old_val != new_val:
                changed_fields.append(field)
    except Track.DoesNotExist:
        pass

    track, created = Track.objects.update_or_create(
        file_path=tag_data["file_path"],
        defaults=defaults,
    )
    if created and source:
        track.source = source
        track.save(update_fields=["source"])
    return created, changed_fields


def _count_files(library_path: str, extensions: set[str]) -> int:
    """Count total audio files in the library."""
    count = 0
    for dirpath, _dirnames, filenames in os.walk(library_path):
        for filename in filenames:
            if filename.startswith("._"):
                continue
            ext = Path(filename).suffix.lstrip(".").lower()
            if ext in extensions:
                count += 1
    return count


def scan(force: bool = False, clean: bool = False, progress_callback=None) -> dict:
    """Scan the music library and return stats.

    Args:
        force: Re-read tags even if mtime hasn't changed.
        clean: Remove DB entries whose files no longer exist on disk.
        progress_callback: Optional callable(current, total, label) called for progress.
    """
    library_path = settings.MUSIC_LIBRARY_PATH
    extensions = settings.MUSIC_EXTENSIONS

    total_files = _count_files(library_path, extensions)
    stats = {"scanned": 0, "created": 0, "updated": 0, "skipped": 0, "errors": 0, "error_files": [], "updated_files": [], "total": total_files}

    seen_paths: set[str] = set()

    scan_start = time.monotonic()
    for dirpath, _dirnames, filenames in os.walk(library_path):
        for filename in filenames:
            if filename.startswith("._"):
                continue
            ext = Path(filename).suffix.lstrip(".").lower()
            if ext not in extensions:
                continue

            filepath = os.path.join(dirpath, filename)
            seen_paths.add(filepath)
            stats["scanned"] += 1

            if progress_callback:
                progress_callback(stats["scanned"], total_files, "Scanning files")

            # Check if we can skip
            if not force:
                try:
                    existing = Track.objects.get(file_path=filepath)
                    current_mtime = os.stat(filepath).st_mtime
                    if existing.file_mtime == current_mtime:
                        stats["skipped"] += 1
                        continue
                except Track.DoesNotExist:
                    pass

            tag_data = read_tags(filepath)
            if tag_data is None:
                stats["errors"] += 1
                stats["error_files"].append(filepath)
                continue

            created, changed_fields = _upsert_track(tag_data, source="local filesystem")
            if created:
                stats["created"] += 1
            elif changed_fields:
                stats["updated"] += 1
                stats["updated_files"].append((filepath, changed_fields))
            else:
                stats["skipped"] += 1

    stats["scan_duration"] = time.monotonic() - scan_start

    # Check cover art status for all albums
    stats["cover_invalid"] = 0
    stats["cover_invalid_albums"] = []
    all_albums = list(Album.objects.all())
    total_albums = len(all_albums)
    cover_start = time.monotonic()
    for i, album in enumerate(all_albums, 1):
        if progress_callback:
            progress_callback(i, total_albums, "Checking cover art")
        status = check_cover_status(album)
        if status == Album.COVER_INVALID:
            stats["cover_invalid"] += 1
            stats["cover_invalid_albums"].append(str(album))
    stats["cover_duration"] = time.monotonic() - cover_start

    if clean:
        stale = Track.objects.exclude(file_path__in=seen_paths)
        stale_count = stale.count()
        stale.delete()
        stats["cleaned_tracks"] = stale_count

        # Remove orphan albums and artists
        orphan_albums = Album.objects.filter(tracks__isnull=True)
        stats["cleaned_albums"] = orphan_albums.count()
        orphan_albums.delete()

        orphan_artists = Artist.objects.filter(
            albums__isnull=True, tracks__isnull=True, album_artist_tracks__isnull=True
        )
        stats["cleaned_artists"] = orphan_artists.count()
        orphan_artists.delete()

    return stats
