from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path


def get_metadata_from_ytdl(url: str) -> dict:
    """Fetch album metadata from a YouTube Music URL.

    Returns dict with keys: album, artist, tracks (list of track metadata dicts).
    """
    cmd = [
        "yt-dlp", "--flat-playlist", "--dump-json", url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp metadata fetch failed: {result.stderr}")

    tracks = []
    album_title = ""
    artist_name = ""
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
        tracks.append({
            "title": meta.get("title", ""),
            "track_number": meta.get("playlist_index"),
            "duration": meta.get("duration"),
            "url": meta.get("url") or meta.get("webpage_url") or "",
        })

    return {"album": album_title, "artist": artist_name, "tracks": tracks}


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

    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp exited with code {result.returncode}")

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
