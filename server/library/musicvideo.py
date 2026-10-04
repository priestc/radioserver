"""Download music videos from YouTube into the library.

Each video is saved as an mp4 under ``<library>/Music Videos/<Artist>/``, and
its audio is extracted to an m4a in the normal ``<Artist>/<Album>/`` layout so
it becomes an ordinary Track (and therefore plays in the radio clients). The
Track's ``video_path`` points back at the mp4.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

MUSIC_VIDEOS_DIRNAME = "Music Videos"
DEFAULT_ALBUM = "Music Videos"

# Prefer H.264 + AAC in mp4 so the video plays natively in browsers and on
# iOS, and the audio can be extracted without re-encoding.
VIDEO_FORMAT = (
    "bv*[vcodec^=avc1]+ba[ext=m4a]/"
    "bv*[ext=mp4]+ba[ext=m4a]/"
    "b[ext=mp4]/"
    "bv*+ba/b"
)


def _strip_topic(name: str) -> str:
    if name.endswith(" - Topic"):
        return name[: -len(" - Topic")]
    return name


def _safe_filename(name: str) -> str:
    """Make a string safe to use as a single path component."""
    name = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", name).strip().strip(".")
    return name[:200] or "Untitled"


def _guess_artist_title(meta: dict) -> tuple[str, str]:
    """Best-effort artist/title from yt-dlp metadata.

    YouTube Music videos carry ``artist``/``track``; regular uploads usually
    have a title like "Artist - Song (Official Video)".
    """
    artist = meta.get("artist") or ""
    title = meta.get("track") or ""
    raw_title = meta.get("title") or ""

    if not (artist and title) and " - " in raw_title:
        left, right = raw_title.split(" - ", 1)
        artist = artist or left.strip()
        title = title or right.strip()

    title = title or raw_title
    # Drop common video-specific suffixes like "(Official Music Video)"
    title = re.sub(
        r"\s*[\(\[][^\)\]]*\b(official|music video|video|lyric|hd|4k|remaster(ed)?)\b[^\)\]]*[\)\]]",
        "", title, flags=re.IGNORECASE,
    ).strip() or title

    artist = artist or _strip_topic(meta.get("channel") or meta.get("uploader") or "")
    return _strip_topic(artist), title


def get_video_metadata(url: str) -> dict:
    """Fetch metadata for a video URL (or every video in a playlist URL).

    Returns {"videos": [...], "errors": [...]}.
    """
    from library.ytdl import _best_thumbnail

    cmd = ["yt-dlp", "--dump-json", "--yes-playlist", "--ignore-errors", "--skip-download", url]
    result = subprocess.run(cmd, capture_output=True, text=True)

    stderr = result.stderr.strip()
    error_lines = [l for l in stderr.splitlines() if "ERROR" in l]

    if not result.stdout.strip():
        detail = error_lines[-1] if error_lines else stderr[-500:] if stderr else "(no output)"
        raise RuntimeError(f"yt-dlp metadata fetch failed: {detail}")

    videos = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        meta = json.loads(line)
        artist, title = _guess_artist_title(meta)
        year = meta.get("release_year")
        if not year:
            upload_date = meta.get("upload_date") or ""
            year = int(upload_date[:4]) if upload_date[:4].isdigit() else None
        videos.append({
            "url": meta.get("webpage_url") or meta.get("original_url") or url,
            "title": title,
            "artist": artist,
            "album": meta.get("album") or "",
            "genre": meta.get("genre") or "",
            "year": year,
            "duration": meta.get("duration"),
            "thumbnail": _best_thumbnail(meta),
            "height": meta.get("height"),
        })

    return {"videos": videos, "errors": error_lines}


def _download_video(url: str, dest_dir: Path) -> Path:
    """Download a single video as mp4 into dest_dir and return its path."""
    cmd = [
        "yt-dlp",
        "-f", VIDEO_FORMAT,
        "--merge-output-format", "mp4",
        "--no-playlist",
        "-o", str(dest_dir / "video.%(ext)s"),
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    videos = [f for f in dest_dir.iterdir() if f.is_file() and f.suffix.lower() in {".mp4", ".mkv", ".webm"}]
    if result.returncode != 0 or not videos:
        stderr = result.stderr.strip()
        error_lines = [l for l in stderr.splitlines() if "ERROR" in l]
        detail = error_lines[-1] if error_lines else stderr[-500:] if stderr else "(no output)"
        raise RuntimeError(f"yt-dlp video download failed: {detail}")
    return videos[0]


def _audio_codec(path: Path) -> str:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path.name}: {result.stderr.strip()[-500:]}")
    codec = result.stdout.strip()
    if not codec:
        raise RuntimeError(f"Video {path.name} has no audio stream")
    return codec


def _extract_audio(video: Path, dest: Path) -> None:
    """Extract the audio track to an m4a, copying the stream when it is already AAC."""
    codec = _audio_codec(video)
    audio_args = ["-c:a", "copy"] if codec == "aac" else ["-c:a", "aac", "-b:a", "256k"]
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(video), "-vn", "-map", "0:a:0", *audio_args, str(dest)],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not dest.exists():
        raise RuntimeError(f"ffmpeg audio extraction failed: {result.stderr.strip()[-500:]}")


def _write_audio_tags(path: Path, dl) -> None:
    from mutagen import File as MutagenFile

    audio = MutagenFile(str(path), easy=True)
    if audio is None:
        raise RuntimeError(f"Could not open extracted audio {path.name} for tagging")
    if audio.tags is None:
        audio.add_tags()
    audio.tags["title"] = [dl.title]
    audio.tags["artist"] = [dl.artist_name]
    audio.tags["albumartist"] = [dl.artist_name]
    audio.tags["album"] = [dl.album_title or DEFAULT_ALBUM]
    if dl.genre:
        audio.tags["genre"] = [dl.genre]
    if dl.year:
        audio.tags["date"] = [str(dl.year)]
    audio.save()


def run_video_download(download_id: int) -> None:
    """Run the full pipeline for a MusicVideoDownload. Runs in a background thread."""
    from django.conf import settings
    from django.db import connection

    connection.close()

    from library.models import MusicVideoDownload, Track

    dl = MusicVideoDownload.objects.get(pk=download_id)

    try:
        library_root = Path(settings.MUSIC_LIBRARY_PATH)
        artist_dir = _safe_filename(dl.artist_name)
        album_dir = _safe_filename(dl.album_title or DEFAULT_ALBUM)
        base_name = _safe_filename(f"{dl.artist_name} - {dl.title}")

        video_dir = library_root / MUSIC_VIDEOS_DIRNAME / artist_dir
        video_dest = video_dir / f"{base_name}.mp4"
        audio_dir = library_root / artist_dir / album_dir
        audio_dest = audio_dir / f"{_safe_filename(dl.title)}.m4a"

        if video_dest.exists():
            raise RuntimeError(f"Video already exists in library: {video_dest}")
        if audio_dest.exists():
            raise RuntimeError(f"Audio file already exists in library: {audio_dest}")

        # Step 1: download video
        dl.status = "downloading"
        dl.progress_message = "Downloading video..."
        dl.save(update_fields=["status", "progress_message"])

        tmp_dir = Path(tempfile.mkdtemp(prefix="mvdl_", dir=Path.home()))
        try:
            downloaded = _download_video(dl.url, tmp_dir)
            if downloaded.suffix.lower() != ".mp4":
                video_dest = video_dest.with_suffix(downloaded.suffix.lower())

            # Step 2: extract audio
            dl.status = "extracting"
            dl.progress_message = "Extracting audio..."
            dl.save(update_fields=["status", "progress_message"])

            tmp_audio = tmp_dir / "audio.m4a"
            _extract_audio(downloaded, tmp_audio)
            _write_audio_tags(tmp_audio, dl)

            video_dir.mkdir(parents=True, exist_ok=True)
            audio_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(downloaded), str(video_dest))
            shutil.move(str(tmp_audio), str(audio_dest))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        dl.video_path = str(video_dest)
        dl.save(update_fields=["video_path"])

        if not (audio_dir / "folder.jpg").exists() and dl.thumbnail:
            from library.ytdl import _download_thumbnail
            dl.progress_message = "Saving cover art..."
            dl.save(update_fields=["progress_message"])
            _download_thumbnail(dl.thumbnail, audio_dir)

        # Step 3: add the audio to the library (just this file, no full scan)
        dl.status = "scanning"
        dl.progress_message = "Adding track to library..."
        dl.save(update_fields=["status", "progress_message"])

        from library.scanner import _upsert_track
        from library.tags import read_tags

        tag_data = read_tags(audio_dest)
        if tag_data is None:
            raise RuntimeError(f"Could not read tags from {audio_dest}")
        _upsert_track(tag_data, artist_dir, source=dl.url)
        track = Track.objects.get(file_path=str(audio_dest))
        track.video_path = str(video_dest)
        track.save(update_fields=["video_path"])

        # Step 4: ReplayGain
        dl.status = "applying_replaygain"
        dl.progress_message = "Applying ReplayGain..."
        dl.save(update_fields=["status", "progress_message"])

        from library.management.commands.replaygain import (
            _analyze_loudness, _compute_gain, _write_replaygain_tags,
        )

        rg_msg = "ReplayGain applied."
        loudness = _analyze_loudness(track.file_path)
        if loudness is None:
            rg_msg = "ReplayGain analysis failed (track imported without it)."
        elif not _write_replaygain_tags(track.file_path, _compute_gain(loudness["input_i"]), loudness["input_tp"]):
            rg_msg = "Writing ReplayGain tags failed (track imported without it)."

        dl.status = "complete"
        dl.progress_message = f"Done. Video saved to {video_dest}\nAudio added as {audio_dest}\n{rg_msg}"
        dl.track = track
        dl.save(update_fields=["status", "progress_message", "track"])

    except Exception as e:
        dl.status = "error"
        dl.error_message = str(e)
        dl.save(update_fields=["status", "error_message"])
