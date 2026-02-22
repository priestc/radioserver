from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from library.models import Album
from library.scanner import scan


class Command(BaseCommand):
    help = "Download an album from YouTube Music and import it into the library."

    def add_arguments(self, parser):
        parser.add_argument("url", help="YouTube Music album/playlist URL")

    def _crop_to_square(self, path: Path):
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
        self.stdout.write(f"  Cropped {path.name} from {w}x{h} to {side}x{side}")

    def handle(self, **options):
        url = options["url"]

        version = subprocess.run(
            ["yt-dlp", "--version"], capture_output=True, text=True,
        )
        if version.returncode != 0:
            raise CommandError("yt-dlp is not installed or not on PATH")
        self.stdout.write(f"yt-dlp {version.stdout.strip()}")

        # Fetch metadata to check for duplicates before downloading
        artist_name = ""
        meta_cmd = [
            "yt-dlp", "--flat-playlist", "--dump-json",
            "--playlist-items", "1", url,
        ]
        meta_result = subprocess.run(meta_cmd, capture_output=True, text=True)
        if meta_result.returncode == 0 and meta_result.stdout.strip():
            meta = json.loads(meta_result.stdout.strip().split("\n")[0])
            album_title = meta.get("album") or meta.get("playlist_title") or ""
            artist_name = meta.get("artist") or meta.get("channel") or ""
            if artist_name.endswith(" - Topic"):
                artist_name = artist_name[: -len(" - Topic")]
            if album_title and artist_name:
                if Album.objects.filter(
                    title__iexact=album_title, artist__name__iexact=artist_name,
                ).exists():
                    raise CommandError(
                        f"Album already in library: {artist_name} — {album_title}"
                    )
                self.stdout.write(f"Album not yet in library: {artist_name} — {album_title}")

        library_dir = Path(settings.MUSIC_LIBRARY_PATH) / (artist_name or "from youtube music")
        library_dir.mkdir(parents=True, exist_ok=True)

        # Download to a temp dir under ~ so snap yt-dlp has access
        tmp_dir = Path(tempfile.mkdtemp(prefix="ytdl_", dir=Path.home()))
        try:
            album_dir_template = "%(album,playlist_title)s"
            output_template = str(tmp_dir / album_dir_template / "%(track_number)02d %(title)s.%(ext)s")

            cmd = [
                "yt-dlp",
                "-x", "--audio-format", "mp3",
                "-f", "bestaudio[abr<=192]/bestaudio",
                "--embed-thumbnail",
                "--add-metadata",
                "--parse-metadata", "playlist_index:%(track_number)s",
                "--yes-playlist",
                "-o", output_template,
                url,
            ]

            self.stdout.write(f"Downloading to {tmp_dir} ...")
            self.stdout.write(f"Running: {' '.join(cmd)}\n")

            result = subprocess.run(cmd)
            if result.returncode != 0:
                raise CommandError(f"yt-dlp exited with code {result.returncode}")

            # Extract cover art from an mp3 and clean up, then move to library
            for item in tmp_dir.iterdir():
                if not item.is_dir():
                    continue

                # Extract embedded thumbnail from the first mp3 as folder.jpg
                cover = item / "folder.jpg"
                if not cover.exists():
                    for mp3 in item.glob("*.mp3"):
                        extract = subprocess.run(
                            ["ffmpeg", "-i", str(mp3), "-an", "-vcodec", "mjpeg",
                             "-frames:v", "1", str(cover)],
                            capture_output=True,
                        )
                        if extract.returncode == 0 and cover.exists():
                            self.stdout.write(f"  Extracted cover art from {mp3.name}")
                            break

                if cover.exists():
                    self._crop_to_square(cover)

                # Remove any stray image files (keep only folder.jpg and mp3s)
                for f in item.iterdir():
                    if f.suffix.lower() in (".jpg", ".png", ".webp") and f.name != "folder.jpg":
                        f.unlink()
                        self.stdout.write(f"  Removed {f.name}")

                dest = library_dir / item.name
                if dest.exists():
                    # Merge files into existing album directory
                    for f in item.iterdir():
                        shutil.move(str(f), str(dest / f.name))
                    self.stdout.write(f"  Merged into {dest}")
                else:
                    shutil.move(str(item), str(dest))
                    self.stdout.write(f"  Moved to {dest}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        # Scan the library to import new tracks
        self.stdout.write("\nScanning library...")
        stats = scan()
        self.stdout.write(f"  Created: {stats['created']}")
        self.stdout.write(f"  Updated: {stats['updated']}")
        self.stdout.write(self.style.SUCCESS("Done."))
