from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

from library.models import VARIOUS_ARTISTS

YT_CREDIT = "Provided to YouTube by"
SUPPORTED_EXTS = {"mp3", "flac", "m4a", "aac", "ogg", "opus", "wav"}


def youtube_credit(path: Path) -> dict | None:
    """The label's own credit from an auto-generated YouTube Music description.

    yt-dlp --add-metadata stores the video description in the file, and for
    label uploads it reads "Provided to YouTube by <label>", then
    "<title> · <artist> · <artist>...", then the album. That's the reliable
    artist: the playlist's artist/channel fields are sometimes junk (spam
    channel names in Korean, or "Release"). Returns {"title", "artists", "album"}
    or None when the file has no such description.
    """
    import mutagen

    try:
        audio = mutagen.File(str(path))
    except mutagen.MutagenError:
        return None
    if audio is None or not audio.tags:
        return None
    for _key, value in audio.tags.items():
        for text in (value if isinstance(value, list) else [value]):
            text = str(text)
            if YT_CREDIT not in text:
                continue
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            i = next(i for i, l in enumerate(lines) if YT_CREDIT in l)
            if i + 1 >= len(lines) or " · " not in lines[i + 1]:
                return None
            title, *artists = [p.strip() for p in lines[i + 1].split(" · ")]
            return {"title": title, "artists": [a for a in artists if a],
                    "album": lines[i + 2] if i + 2 < len(lines) else ""}
    return None


def album_artist_for(track_artists: list[str]) -> str:
    """One artist with more than half of an album's tracks is its album artist;
    otherwise it's a compilation, filed under Various Artists."""
    firsts = [a.split(",")[0].strip() for a in track_artists if a.strip()]
    if not firsts:
        return ""
    artist, n = Counter(firsts).most_common(1)[0]
    return artist if n * 2 > len(firsts) else VARIOUS_ARTISTS


def resolve_credits(files: list[Path], overrides: dict[int, dict] | None = None) -> dict[Path, str]:
    """Settle each downloaded file's artist and album artist and write them into its tags.

    Artist: what the user typed on the download page, else the YouTube credit,
    else what yt-dlp/the page already put in the file. Album artist: what the
    user typed, else album_artist_for() over the album's tracks, so a
    compilation becomes one Various Artists album instead of one album per
    artist. Returns {file: album artist}, the library folder each belongs in.
    """
    import mutagen
    from mutagen import File as MutagenFile

    overrides = overrides or {}
    info = {}
    for f in files:
        try:
            audio = MutagenFile(str(f), easy=True)
        except mutagen.MutagenError:
            audio = None
        tags = audio.tags if audio is not None and audio.tags is not None else {}
        ov = overrides.get(_track_number(f), {})
        credit = youtube_credit(f)
        artist = ((ov.get("artist") or "").strip() if ov.get("artist_edited") else "") \
            or (", ".join(credit["artists"]) if credit and credit["artists"] else "") \
            or (ov.get("artist") or "").strip() or (tags.get("artist") or [""])[0]
        album = (ov.get("album") or "").strip() or (tags.get("album") or [""])[0] or f.parent.name
        typed_album_artist = (ov.get("album_artist") or "").strip() if ov.get("album_artist_edited") else ""
        info[f] = {"audio": audio, "artist": artist, "album": album, "album_artist": typed_album_artist}

    by_album: dict[str, list[Path]] = {}
    for f, d in info.items():
        by_album.setdefault(d["album"], []).append(f)
    result = {}
    for album, album_files in by_album.items():
        guess = album_artist_for([info[f]["artist"] for f in album_files])
        for f in album_files:
            d = info[f]
            album_artist = d["album_artist"] or guess or d["artist"] or "Unknown Artist"
            result[f] = album_artist
            if d["audio"] is None:
                continue
            if d["audio"].tags is None:
                d["audio"].add_tags()
            if d["artist"]:
                d["audio"].tags["artist"] = [d["artist"]]
            d["audio"].tags["albumartist"] = [album_artist]
            d["audio"].save()
    return result


def _track_number(path: Path) -> int | None:
    """Track number from a downloaded file's "NN title.ext" name."""
    try:
        return int(path.stem.split(" ", 1)[0])
    except (ValueError, IndexError):
        return None


def _apply_track_overrides(tmp_dir: Path, track_overrides: list[dict]) -> None:
    """Write track metadata overrides into downloaded audio file tags.

    Matches files by track number parsed from the filename (format: "NN title.ext").
    """
    import mutagen
    from mutagen import File as MutagenFile

    overrides = {}
    for ov in track_overrides:
        tn = ov.get("track_number")
        if tn is not None:
            overrides[tn] = ov

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
        if ov.get("album_artist", "").strip():
            audio.tags["albumartist"] = [ov["album_artist"].strip()]
        if ov.get("genre", "").strip():
            audio.tags["genre"] = [ov["genre"].strip()]

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


def _album_from_meta(meta: dict) -> str:
    """Album name from yt-dlp metadata.

    Flat-playlist entries have no ``album`` field, so fall back to the playlist
    title — which YouTube Music formats as "Album - <name>".
    """
    album = meta.get("album") or ""
    if album:
        return album
    playlist_title = meta.get("playlist_title") or ""
    if playlist_title.startswith("Album - "):
        playlist_title = playlist_title[len("Album - "):]
    return playlist_title


def _track_artists(urls: list[str]) -> dict[str, tuple[str, str | None]]:
    """Each track's credited artist, from its full yt-dlp metadata.

    Flat-playlist entries only name the uploading channel ("RideOfficial",
    "Slowdive Official"), not the artist, so each video is looked up on its
    own, several at a time. Returns {url: (artist, error or None)}.
    """
    from concurrent.futures import ThreadPoolExecutor

    def lookup(url: str) -> tuple[str, str | None]:
        result = subprocess.run(
            ["yt-dlp", "-j", "--skip-download", "--no-playlist", url],
            capture_output=True, text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            errors = [l for l in result.stderr.splitlines() if "ERROR" in l]
            return "", (errors[-1] if errors else "couldn't look up the track's artist")
        meta = json.loads(result.stdout)
        artists = meta.get("artists") or meta.get("creators") or []
        return ", ".join(artists) or meta.get("artist") or meta.get("creator") or "", None

    urls = [u for u in dict.fromkeys(urls) if u]
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(zip(urls, pool.map(lookup, urls)))


def get_metadata_from_ytdl(url: str) -> dict:
    """Fetch album metadata from a YouTube Music URL.

    Returns dict with keys: album, artist, tracks (list of track metadata dicts).
    """
    cmd = [
        "yt-dlp", "--dump-json", "--flat-playlist", "--yes-playlist", "--ignore-errors", url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)

    stderr = result.stderr.strip()
    error_lines = [l for l in stderr.splitlines() if "ERROR" in l]

    # yt-dlp returns non-zero when some videos are unavailable but still
    # outputs JSON for the ones that worked. Only fail if we got no output.
    if not result.stdout.strip():
        detail = error_lines[-1] if error_lines else stderr[-500:] if stderr else "(no output)"
        raise RuntimeError(f"yt-dlp metadata fetch failed: {detail}")

    entries = [json.loads(line) for line in result.stdout.strip().split("\n") if line]
    credits = _track_artists([m.get("url") or m.get("webpage_url") or "" for m in entries])

    tracks = []
    album_title = ""
    seen_thumbnails = []
    seen_thumbnail_urls = set()
    for meta in entries:
        if not album_title:
            album_title = _album_from_meta(meta)

        # Extract best thumbnail for this track
        thumb_url = _best_thumbnail(meta)
        if thumb_url and thumb_url not in seen_thumbnail_urls:
            seen_thumbnail_urls.add(thumb_url)
            seen_thumbnails.append({
                "url": thumb_url,
                "track_title": meta.get("title", ""),
            })

        url = meta.get("url") or meta.get("webpage_url") or ""
        credit, error = credits.get(url, ("", None))
        if error:
            error_lines.append(f"{meta.get('title', url)}: {error}")
        # The uploading channel ("RideOfficial") is only a last resort.
        track_artist = credit or meta.get("artist") or meta.get("channel") or ""
        if track_artist.endswith(" - Topic"):
            track_artist = track_artist[: -len(" - Topic")]

        tracks.append({
            "title": meta.get("title", ""),
            "track_number": meta.get("playlist_index"),
            "duration": meta.get("duration"),
            "url": url,
            "thumbnail": thumb_url,
            "album": _album_from_meta(meta),
            "artist": track_artist,
            "album_artist": meta.get("album_artist") or "",
            "genre": meta.get("genre") or "",
        })

    # The page pre-fills each track's album artist from its album's track artists;
    # a compilation's tracks all get Various Artists. (resolve_credits() re-checks
    # against each file's YouTube credit after downloading.)
    albums: dict[str, list[dict]] = {}
    for t in tracks:
        albums.setdefault(t["album"], []).append(t)
    for album_tracks in albums.values():
        guess = album_artist_for([t["artist"] for t in album_tracks])
        for t in album_tracks:
            t["album_artist"] = t["album_artist"] or guess or t["artist"]
    artist_name = tracks[0]["album_artist"] if tracks else ""
    if tracks and all(t["album_artist"] == VARIOUS_ARTISTS for t in tracks):
        artist_name = VARIOUS_ARTISTS

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
        "-f", "bestaudio/best",
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
            "-f", "bestaudio/best",
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

        library_root = Path(settings.MUSIC_LIBRARY_PATH)

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

            # Organise each track into album_artist/album/
            dl.progress_message = "Organizing tracks by album artist/album..."
            dl.save(update_fields=["progress_message"])

            overrides = {}
            if dl.track_overrides:
                for ov in dl.track_overrides:
                    tn = ov.get("track_number")
                    if tn is not None:
                        overrides[tn] = ov

            audio_files = sorted(
                f for f in tmp_dir.rglob("*")
                if f.suffix.lstrip(".").lower() in SUPPORTED_EXTS
            )
            folder_artists = resolve_credits(audio_files, overrides)

            art_extracted = set()

            for audio_file in audio_files:
                track_num = _track_number(audio_file)
                ov = overrides.get(track_num, {}) if track_num else {}
                folder_artist = folder_artists[audio_file]
                track_album = ov.get("album", "").strip() or dl.album_title or "Unknown Album"

                dest_dir = library_root / folder_artist / track_album
                dest_dir.mkdir(parents=True, exist_ok=True)
                affected_dirs.add(dest_dir)

                if str(dest_dir) not in art_extracted:
                    art_extracted.add(str(dest_dir))
                    if not (dest_dir / "folder.jpg").exists():
                        dl.progress_message = f"Extracting album art for {folder_artist}/{track_album}..."
                        dl.save(update_fields=["progress_message"])
                        thumb_url = ov.get("thumbnail", "")
                        if not thumb_url or not _download_thumbnail(thumb_url, dest_dir):
                            get_albumart_from_ytdl(dl.url, dest_dir)

                shutil.move(str(audio_file), str(dest_dir / audio_file.name))
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

        # Find the album that was created (the first track's folder)
        album = None
        if audio_files:
            first_dir = library_root / folder_artists[audio_files[0]]
            dl.artist_name = folder_artists[audio_files[0]]
            album = Album.objects.filter(
                artist__name=dl.artist_name,
                tracks__file_path__startswith=str(first_dir) + "/",
            ).first()

        # Build completion message
        msg_parts = [f"Done. {stats['created']} tracks imported, {rg_ok} ReplayGain tagged."]

        # Identify which tracks failed to download
        if dl.track_overrides and download_errors:
            # Parse track numbers from downloaded files
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
            for ov in dl.track_overrides:
                track_num = ov.get("track_number")
                if track_num is not None and track_num not in downloaded_nums:
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
        dl.save(update_fields=["status", "progress_message", "album", "artist_name"])

    except Exception as e:
        dl.status = "error"
        dl.error_message = str(e)
        dl.save(update_fields=["status", "error_message"])
