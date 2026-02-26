from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def _apply_track_overrides(tmp_dir: Path, track_overrides: list[dict]) -> None:
    """Write track metadata overrides into downloaded audio file tags.

    Matches files by track number parsed from the filename (format: "NN title.ext").
    """
    import mutagen
    from mutagen import File as MutagenFile

    SUPPORTED_EXTS = {"mp3", "flac", "m4a", "aac", "ogg", "opus", "wav"}
    overrides = {i + 1: ov for i, ov in enumerate(track_overrides)}

    for audio_file in tmp_dir.rglob("*"):
        if audio_file.suffix.lstrip(".").lower() not in SUPPORTED_EXTS:
            continue

        parts = audio_file.stem.split(" ", 1)
        try:
            track_num = int(parts[0])
        except (ValueError, IndexError):
            continue

        ov = overrides.get(track_num)
        if not ov:
            continue

        try:
            audio = MutagenFile(str(audio_file), easy=True)
        except mutagen.MutagenError:
            continue
        if audio is None:
            continue
        if audio.tags is None:
            audio.add_tags()

        if ov.get("title", "").strip():
            audio.tags["title"] = [ov["title"].strip()]
        if ov.get("artist", "").strip():
            audio.tags["artist"] = [ov["artist"].strip()]
        if ov.get("album", "").strip():
            audio.tags["album"] = [ov["album"].strip()]

        try:
            audio.save()
        except mutagen.MutagenError:
            continue


def _download_thumbnail(url: str, dest_dir: Path) -> Path | None:
    """Download a thumbnail from a URL and save as folder.jpg in dest_dir.

    Downloads the image, crops to square, and saves as folder.jpg.
    Returns the path to folder.jpg or None if download failed.
    """
    import urllib.request

    dest = dest_dir / "folder.jpg"
    tmp_path = dest_dir / "folder_tmp.jpg"
    try:
        urllib.request.urlretrieve(url, str(tmp_path))
        # Convert to JPEG and crop to square via ffmpeg
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(tmp_path), "-frames:v", "1", str(dest)],
            capture_output=True,
        )
        tmp_path.unlink(missing_ok=True)
        if result.returncode != 0 or not dest.exists():
            return None
        _crop_to_square(dest)
        return dest
    except Exception:
        tmp_path.unlink(missing_ok=True)
        return None


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
        "yt-dlp", "--dump-json", "--yes-playlist", "--ignore-errors", url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)

    stderr = result.stderr.strip()
    error_lines = [l for l in stderr.splitlines() if "ERROR" in l]

    # yt-dlp returns non-zero when some videos are unavailable but still
    # outputs JSON for the ones that worked. Only fail if we got no output.
    if not result.stdout.strip():
        detail = error_lines[-1] if error_lines else stderr[-500:] if stderr else "(no output)"
        raise RuntimeError(f"yt-dlp metadata fetch failed: {detail}")

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
        "errors": error_lines,
    }


def get_audio_files_from_ytdl(url: str, dest_dir: Path) -> tuple[list[Path], list[str]]:
    """Download audio files from a YouTube Music URL into dest_dir.

    Returns (files, errors) where files is a list of downloaded file paths
    and errors is a list of yt-dlp error lines for tracks that failed.
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
        "--ignore-errors",
        "-o", output_template,
        url,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    stderr = result.stderr.strip()
    error_lines = [l for l in stderr.splitlines() if "ERROR" in l]

    SUPPORTED_EXTS = {"mp3", "flac", "m4a", "aac", "ogg", "opus", "wav"}
    files = []
    for item in dest_dir.rglob("*"):
        if item.suffix.lstrip(".").lower() in SUPPORTED_EXTS:
            files.append(item)
    if not files:
        detail = error_lines[-1] if error_lines else stderr[-500:] if stderr else "(no output)"
        raise RuntimeError(f"yt-dlp downloaded no audio files: {detail}")
    return sorted(files), error_lines


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
        library_root = Path(settings.MUSIC_LIBRARY_PATH)
        library_dir = library_root / artist_name

        library_dir.mkdir(parents=True, exist_ok=True)

        tmp_dir = Path(tempfile.mkdtemp(prefix="ytdl_", dir=Path.home()))
        # Track all library dirs affected (for source tagging + ReplayGain)
        affected_dirs = set()
        download_errors = []
        try:
            _downloaded_files, download_errors = get_audio_files_from_ytdl(dl.url, tmp_dir)

            # Write track overrides into file tags
            if dl.track_overrides:
                dl.progress_message = "Applying track metadata overrides..."
                dl.save(update_fields=["progress_message"])
                _apply_track_overrides(tmp_dir, dl.track_overrides)

            if dl.use_track_albums and dl.track_overrides:
                # Per-track mode: each track goes to its own artist/album folder
                dl.progress_message = "Organizing tracks by per-track artist/album..."
                dl.save(update_fields=["progress_message"])

                # Build override lookup keyed by 1-based track number
                overrides = {i + 1: ov for i, ov in enumerate(dl.track_overrides)}

                # Collect all audio files from all subdirectories
                SUPPORTED_EXTS = {"mp3", "flac", "m4a", "aac", "ogg", "opus", "wav"}
                audio_files = sorted(
                    f for f in tmp_dir.rglob("*")
                    if f.suffix.lstrip(".").lower() in SUPPORTED_EXTS
                )

                # Extract album art once per unique destination dir
                art_extracted = set()

                for audio_file in audio_files:
                    # Parse track number from filename (format: "NN title.ext")
                    fname = audio_file.stem
                    parts = fname.split(" ", 1)
                    try:
                        track_num = int(parts[0])
                    except (ValueError, IndexError):
                        track_num = None

                    ov = overrides.get(track_num, {}) if track_num else {}
                    track_artist = ov.get("artist", "").strip() or artist_name
                    track_album = ov.get("album", "").strip() or dl.album_title or "Unknown Album"

                    dest_dir = library_root / track_artist / track_album
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    affected_dirs.add(dest_dir)

                    # Extract album art for this destination if not yet done
                    if str(dest_dir) not in art_extracted:
                        art_extracted.add(str(dest_dir))
                        if not (dest_dir / "folder.jpg").exists():
                            dl.progress_message = f"Extracting album art for {track_artist}/{track_album}..."
                            dl.save(update_fields=["progress_message"])
                            thumb_url = ov.get("thumbnail", "")
                            if not thumb_url or not _download_thumbnail(thumb_url, dest_dir):
                                get_albumart_from_ytdl(dl.url, dest_dir)

                    shutil.move(str(audio_file), str(dest_dir / audio_file.name))

            else:
                # Standard mode: all tracks go to artist_name/album_subdir/
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
                    affected_dirs.add(dest)

                # If no subdirs were found, library_dir itself is the affected dir
                if not affected_dirs:
                    affected_dirs.add(library_dir)
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
            for adir in affected_dirs:
                Track.objects.filter(source="").filter(
                    file_path__startswith=str(adir),
                ).update(source=dl.url)

        # Step 3: Apply ReplayGain
        dl.status = "applying_replaygain"
        dl.progress_message = "Applying ReplayGain..."
        dl.save(update_fields=["status", "progress_message"])

        from library.management.commands.replaygain import (
            _analyze_loudness, _compute_gain, _write_replaygain_tags,
        )

        new_tracks = []
        for adir in affected_dirs:
            new_tracks.extend(
                Track.objects.filter(file_path__startswith=str(adir))
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

        # Build completion message
        msg_parts = [f"Done. {stats['created']} tracks imported, {rg_ok} ReplayGain tagged."]

        # Identify which tracks failed to download
        if dl.track_overrides and download_errors:
            # Parse track numbers from downloaded files
            SUPPORTED_EXTS = {"mp3", "flac", "m4a", "aac", "ogg", "opus", "wav"}
            downloaded_nums = set()
            for adir in affected_dirs:
                for f in adir.iterdir():
                    if f.suffix.lstrip(".").lower() in SUPPORTED_EXTS:
                        parts = f.stem.split(" ", 1)
                        try:
                            downloaded_nums.add(int(parts[0]))
                        except (ValueError, IndexError):
                            pass

            failed_tracks = []
            for i, ov in enumerate(dl.track_overrides):
                track_num = i + 1
                if track_num not in downloaded_nums:
                    title = ov.get("title", f"Track {track_num}")
                    failed_tracks.append(f"#{track_num} {title}")

            if failed_tracks:
                msg_parts.append(
                    f"\n{len(failed_tracks)} track(s) failed to download:\n"
                    + "\n".join(failed_tracks)
                )
        elif download_errors:
            msg_parts.append(
                f"\n{len(download_errors)} download error(s):\n"
                + "\n".join(download_errors)
            )

        dl.status = "complete"
        dl.progress_message = "\n".join(msg_parts)
        dl.album = album
        dl.save(update_fields=["status", "progress_message", "album"])

    except Exception as e:
        dl.status = "error"
        dl.error_message = str(e)
        dl.save(update_fields=["status", "error_message"])
