from __future__ import annotations

import os
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


def _upsert_track(tag_data: dict) -> bool:
    """Create or update a Track from tag_data. Returns True if created/updated."""
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

    _, created = Track.objects.update_or_create(
        file_path=tag_data["file_path"],
        defaults=defaults,
    )
    return created


def scan(force: bool = False, clean: bool = False) -> dict:
    """Scan the music library and return stats.

    Args:
        force: Re-read tags even if mtime hasn't changed.
        clean: Remove DB entries whose files no longer exist on disk.
    """
    library_path = settings.MUSIC_LIBRARY_PATH
    extensions = settings.MUSIC_EXTENSIONS

    stats = {"scanned": 0, "created": 0, "updated": 0, "skipped": 0, "errors": 0, "error_files": []}

    seen_paths: set[str] = set()

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

            created = _upsert_track(tag_data)
            if created:
                stats["created"] += 1
            else:
                stats["updated"] += 1

    # Check cover art status for all albums
    stats["cover_invalid"] = 0
    stats["cover_invalid_albums"] = []
    for album in Album.objects.all():
        status = check_cover_status(album)
        if status == Album.COVER_INVALID:
            stats["cover_invalid"] += 1
            stats["cover_invalid_albums"].append(str(album))

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
