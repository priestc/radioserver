from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def _best_thumbnail(meta: dict) -> str:
    """Pick the best thumbnail URL from yt-dlp metadata."""
    # yt-dlp provides a 'thumbnails' list sorted by quality, or a single 'thumbnail'
    thumbnails = meta.get("thumbnails")
    if thumbnails:
        # Pick the last (highest quality) entry
        return thumbnails[-1].get("url", "")
    return meta.get("thumbnail", "")


def get_metadata_from_ytdl(url: str) -> dict:
    """Fetch album metadata from a YouTube Music URL.

    Returns dict with keys: album, artist, tracks (list of track metadata dicts).
    """
    cmd = [
        "yt-dlp", "--dump-json", "--yes-playlist", url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp metadata fetch failed: {result.stderr}")

    tracks = []
    album_title = ""
    artist_name = ""
    seen_thumbnails = []
    seen_thumbnail_urls = set()
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        meta = json.loads(line)
        if not album_title:
            album_title = meta.get("album") or meta.get("playlist_title") or ""
        if not artist_name:
            artist_name = meta.get("artist") or meta.get("channel") or ""
            if artist_name.endswith(" - Topic"):
                artist_name = artist_name[: -len(" - Topic")]

        # Extract best thumbnail for this track
        thumb_url = _best_thumbnail(meta)
        if thumb_url and thumb_url not in seen_thumbnail_urls:
            seen_thumbnail_urls.add(thumb_url)
            seen_thumbnails.append({
                "url": thumb_url,
                "track_title": meta.get("title", ""),
            })

        track_artist = meta.get("artist") or meta.get("channel") or ""
        if track_artist.endswith(" - Topic"):
            track_artist = track_artist[: -len(" - Topic")]

        tracks.append({
            "title": meta.get("title", ""),
            "track_number": meta.get("playlist_index"),
            "duration": meta.get("duration"),
            "url": meta.get("url") or meta.get("webpage_url") or "",
            "thumbnail": thumb_url,
            "album": meta.get("album") or meta.get("playlist_title") or "",
            "artist": track_artist,
        })

    return {
        "album": album_title,
        "artist": artist_name,
        "tracks": tracks,
        "thumbnails": seen_thumbnails,
    }


def get_audio_files_from_ytdl(url: str, dest_dir: Path) -> list[Path]:
    """Download audio files from a YouTube Music URL into dest_dir.

    Returns list of downloaded file paths.
    """
    album_dir_template = "%(album,playlist_title)s"
    output_template = str(dest_dir / album_dir_template / "%(track_number)02d %(title)s.%(ext)s")

    # Prefer m4a/opus containers that don't need re-encoding.
    # --audio-format best keeps the native format when it's already a
    # common audio container (m4a, opus, ogg, mp3), avoiding lossy
    # re-encoding.
    cmd = [
        "yt-dlp",
        "-x", "--audio-format", "best",
        "-f", "bestaudio",
        "--add-metadata",
        "--parse-metadata", "playlist_index:%(track_number)s",
        "--yes-playlist",
        "-o", output_template,
        url,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        # Pull out the last ERROR line if present for a concise message
        error_lines = [l for l in stderr.splitlines() if "ERROR" in l]
        detail = error_lines[-1] if error_lines else stderr[-500:] if stderr else "(no output)"
        raise RuntimeError(f"yt-dlp exited with code {result.returncode}: {detail}")

    SUPPORTED_EXTS = {"mp3", "flac", "m4a", "aac", "ogg", "opus", "wav"}
    files = []
    for item in dest_dir.rglob("*"):
        if item.suffix.lstrip(".").lower() in SUPPORTED_EXTS:
            files.append(item)
    return sorted(files)


def get_albumart_from_ytdl(url: str, dest_dir: Path) -> Path | None:
    """Download and extract album art from a YouTube Music URL.

    Downloads to a temp location, extracts the embedded thumbnail from the
    first mp3, crops to square, and saves as folder.jpg in dest_dir.
    Returns the path to folder.jpg or None if extraction failed.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="ytdl_art_", dir=Path.home()))
    try:
        output_template = str(tmp_dir / "art.%(ext)s")
        cmd = [
            "yt-dlp",
            "-x", "--audio-format", "best",
            "--embed-thumbnail",
            "--playlist-items", "1",
            "-o", output_template,
            url,
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            return None

        # Find the downloaded audio file and extract cover
        audio_file = None
        for f in tmp_dir.iterdir():
            if f.is_file() and f.suffix.lower() in {
                ".mp3", ".m4a", ".opus", ".ogg", ".webm", ".flac",
            }:
                audio_file = f
                break

        if audio_file is None:
            return None

        cover = dest_dir / "folder.jpg"
        extract = subprocess.run(
            ["ffmpeg", "-i", str(audio_file), "-an", "-vcodec", "mjpeg",
             "-frames:v", "1", str(cover)],
            capture_output=True,
        )
        if extract.returncode != 0 or not cover.exists():
            return None

        _crop_to_square(cover)
        return cover
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _crop_to_square(path: Path):
    from PIL import Image
    img = Image.open(path)
    w, h = img.size
    if w == h:
        return
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img.save(path)


def run_download(download_id: int) -> None:
    """Run the full download pipeline for a YtdlDownload record.

    Designed to run in a background thread. Updates the model row at each step.
    """
    import django
    from django.conf import settings
    from django.db import connection

    # Ensure clean DB connection in thread
    connection.close()

    from library.models import Album, Track, YtdlDownload

    dl = YtdlDownload.objects.get(pk=download_id)

    try:
        # Step 1: Download audio
        dl.status = "downloading"
        dl.progress_message = "Downloading audio files..."
        dl.save(update_fields=["status", "progress_message"])

        artist_name = dl.artist_name or "from youtube music"
        library_dir = Path(settings.MUSIC_LIBRARY_PATH) / artist_name

        library_dir.mkdir(parents=True, exist_ok=True)

        tmp_dir = Path(tempfile.mkdtemp(prefix="ytdl_", dir=Path.home()))
        try:
            get_audio_files_from_ytdl(dl.url, tmp_dir)

            # Process each album subdirectory
            for item in tmp_dir.iterdir():
                if not item.is_dir():
                    continue

                # Extract album art
                dl.progress_message = "Extracting album art..."
                dl.save(update_fields=["progress_message"])
                get_albumart_from_ytdl(dl.url, item)

                # Remove stray image files (keep only folder.jpg)
                for f in item.iterdir():
                    if f.suffix.lower() in (".jpg", ".png", ".webp") and f.name != "folder.jpg":
                        f.unlink()

                # Move to library
                dest = library_dir / item.name
                if dest.exists():
                    for f in item.iterdir():
                        shutil.move(str(f), str(dest / f.name))
                else:
                    shutil.move(str(item), str(dest))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        # Step 2: Scan library
        dl.status = "scanning"
        dl.progress_message = "Scanning library..."
        dl.save(update_fields=["status", "progress_message"])

        from library.scanner import scan
        stats = scan()

        # Tag newly created tracks with source URL
        if stats["created"] > 0:
            Track.objects.filter(source="").filter(
                file_path__startswith=str(library_dir),
            ).update(source=dl.url)

        # Step 3: Apply ReplayGain
        dl.status = "applying_replaygain"
        dl.progress_message = "Applying ReplayGain..."
        dl.save(update_fields=["status", "progress_message"])

        from library.management.commands.replaygain import (
            _analyze_loudness, _compute_gain, _write_replaygain_tags,
        )

        new_tracks = list(
            Track.objects.filter(file_path__startswith=str(library_dir))
        )
        rg_ok = 0
        for i, track in enumerate(new_tracks, 1):
            dl.progress_message = f"Applying ReplayGain ({i}/{len(new_tracks)})..."
            dl.save(update_fields=["progress_message"])
            loudness = _analyze_loudness(track.file_path)
            if loudness is None:
                continue
            gain_db = _compute_gain(loudness["input_i"])
            if _write_replaygain_tags(track.file_path, gain_db, loudness["input_tp"]):
                rg_ok += 1

        # Find the album that was created
        album = Album.objects.filter(
            artist__name=dl.artist_name,
            tracks__file_path__startswith=str(library_dir),
        ).first()

        dl.status = "complete"
        dl.progress_message = f"Done. {stats['created']} tracks imported, {rg_ok} ReplayGain tagged."
        dl.album = album
        dl.save(update_fields=["status", "progress_message", "album"])

    except Exception as e:
        dl.status = "error"
        dl.error_message = str(e)
        dl.save(update_fields=["status", "error_message"])
