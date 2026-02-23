from __future__ import annotations

import os
import re
from pathlib import Path

import mutagen
from mutagen import File as MutagenFile


def _first(tags: dict, key: str, default: str = "") -> str:
    """Return the first value for *key* from an EasyTags dict, or *default*."""
    vals = tags.get(key)
    if vals:
        return str(vals[0]).strip()
    return default


def _parse_number(value: str) -> int | None:
    """Parse '3/12' or '3' style track/disc numbers, returning the first part."""
    if not value:
        return None
    try:
        return int(value.split("/")[0])
    except (ValueError, IndexError):
        return None


def _parse_total(value: str) -> int | None:
    """Parse '3/12' returning the second part (total), if present."""
    if not value or "/" not in value:
        return None
    try:
        return int(value.split("/")[1])
    except (ValueError, IndexError):
        return None


def _parse_year(value: str) -> int | None:
    """Extract a 4-digit year from strings like '2023' or '2023-05-14'."""
    if not value:
        return None
    try:
        return int(value[:4])
    except (ValueError, IndexError):
        return None


def _extract_year_from_title(title: str) -> int | None:
    """Extract a year from a song title as a fallback when no date tag exists.

    Checks for (in order):
    - Dates with 4-digit years: "10-4-2001", "5/12/1999"
    - Standalone 4-digit years: "Summer of 2001"
    - Apostrophe + 2-digit years: "Spirit of '71" → 1971
    """
    if not title:
        return None

    # Dates containing a 4-digit year (most specific)
    m = re.search(r"\b\d{1,2}[-/.]\d{1,2}[-/.](19\d{2}|20\d{2})\b", title)
    if m:
        return int(m.group(1))

    # Standalone 4-digit year (1900–2099)
    m = re.search(r"\b(19\d{2}|20\d{2})\b", title)
    if m:
        return int(m.group(1))

    # Apostrophe + 2-digit year: '00–'29 → 2000s, '30–'99 → 1900s
    m = re.search(r"'(\d{2})\b", title)
    if m:
        two_digit = int(m.group(1))
        return 2000 + two_digit if two_digit <= 29 else 1900 + two_digit

    return None


FORMAT_MAP = {
    "MP3": "mp3",
    "FLAC": "flac",
    "OggVorbis": "ogg",
    "OggOpus": "opus",
    "MP4": "m4a",
    "AAC": "aac",
    "WavPack": "wav",
    "ASF": "wma",
    "WAV": "wav",  # mutagen calls it WAVE sometimes
}


def write_album_tags(album) -> None:
    """Write album-level metadata to the tags of all tracks in the album.

    Updates: album title, album artist, year, total_tracks, total_discs.
    """
    for track in album.tracks.all():
        try:
            audio = MutagenFile(track.file_path, easy=True)
        except mutagen.MutagenError:
            continue
        if audio is None:
            continue
        if audio.tags is None:
            audio.add_tags()

        audio.tags["album"] = [album.title]
        audio.tags["albumartist"] = [album.artist.name]

        if album.year is not None:
            audio.tags["date"] = [str(album.year)]

        # Write tracknumber as "N/total" if total_tracks is set
        existing_track_num = _first(dict(audio.tags), "tracknumber")
        track_num = _parse_number(existing_track_num)
        if track_num is not None:
            if album.total_tracks is not None:
                audio.tags["tracknumber"] = [f"{track_num}/{album.total_tracks}"]

        # Write discnumber as "N/total" if total_discs is set
        existing_disc_num = _first(dict(audio.tags), "discnumber")
        disc_num = _parse_number(existing_disc_num)
        if disc_num is not None:
            if album.total_discs is not None:
                audio.tags["discnumber"] = [f"{disc_num}/{album.total_discs}"]

        try:
            audio.save()
        except mutagen.MutagenError:
            continue


def read_tags(path: str | Path) -> dict | None:
    """Read audio metadata from *path* and return a normalised dict.

    Returns ``None`` if mutagen cannot open the file.
    """
    path = str(path)
    try:
        audio = MutagenFile(path, easy=True)
    except mutagen.MutagenError:
        return None
    if audio is None:
        return None

    tags = dict(audio.tags) if audio.tags else {}
    info = audio.info

    # Determine format from mutagen type name
    type_name = type(audio).__name__
    fmt = FORMAT_MAP.get(type_name, Path(path).suffix.lstrip(".").lower())

    track_raw = _first(tags, "tracknumber")
    disc_raw = _first(tags, "discnumber")

    stat = os.stat(path)

    # Split all artist tag values on commas, slashes, or "Ft."/"Feat."/"Featuring"
    # The & delimiter is only recognised after a feat/ft/featuring split has occurred
    raw_artists = tags.get("artist", [])
    artists = []
    for val in raw_artists:
        for part in re.split(r"[,/]|\bFt\.?\b|\bFeat\.?\b|\bFeaturing\b|\bDuet\s+with\b|\bwith\b", str(val), flags=re.IGNORECASE):
            part = part.strip()
            if not part:
                continue
            # If this part came after a feat delimiter, split on & too
            if artists:
                for name in part.split("&"):
                    name = name.strip()
                    if name:
                        artists.append(name)
            else:
                artists.append(part)
    if not artists:
        artists = ["Unknown Artist"]

    return {
        "title": _first(tags, "title") or Path(path).stem,
        "artists": artists,
        "album": _first(tags, "album") or "Unknown Album",
        "track_number": _parse_number(track_raw),
        "disc_number": _parse_number(disc_raw),
        "total_tracks": _parse_total(track_raw),
        "total_discs": _parse_total(disc_raw),
        "genre": _first(tags, "genre"),
        "year": _parse_year(_first(tags, "date")),
        "year_from_title": _extract_year_from_title(_first(tags, "title")),
        "duration": getattr(info, "length", None),
        "bitrate": getattr(info, "bitrate", None),
        "sample_rate": getattr(info, "sample_rate", None),
        "channels": getattr(info, "channels", None),
        "format": fmt,
        "file_path": path,
        "file_size": stat.st_size,
        "file_mtime": stat.st_mtime,
    }
