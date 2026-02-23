from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from library.models import Album, Track
from library.scanner import scan
from library.ytdl import get_albumart_from_ytdl, get_audio_files_from_ytdl, get_metadata_from_ytdl


class Command(BaseCommand):
    help = "Download an album from YouTube Music and import it into the library."

    def add_arguments(self, parser):
        parser.add_argument("url", help="YouTube Music album/playlist URL")

    def handle(self, **options):
        url = options["url"]

        version = subprocess.run(
            ["yt-dlp", "--version"], capture_output=True, text=True,
        )
        if version.returncode != 0:
            raise CommandError("yt-dlp is not installed or not on PATH")
        self.stdout.write(f"yt-dlp {version.stdout.strip()}")

        # Fetch metadata to check for duplicates before downloading
        self.stdout.write("Fetching metadata...")
        try:
            metadata = get_metadata_from_ytdl(url)
        except RuntimeError as e:
            raise CommandError(str(e))

        artist_name = metadata["artist"]
        album_title = metadata["album"]

        if album_title and artist_name:
            if Album.objects.filter(
                title__iexact=album_title, artist__name__iexact=artist_name,
            ).exists():
                raise CommandError(
                    f"Album already in library: {artist_name} — {album_title}"
                )
            self.stdout.write(f"Album not yet in library: {artist_name} — {album_title}")
            self.stdout.write(f"  {len(metadata['tracks'])} tracks found")

        library_dir = Path(settings.MUSIC_LIBRARY_PATH) / (artist_name or "from youtube music")
        library_dir.mkdir(parents=True, exist_ok=True)

        # Download audio to a temp dir under ~ so snap yt-dlp has access
        tmp_dir = Path(tempfile.mkdtemp(prefix="ytdl_", dir=Path.home()))
        try:
            self.stdout.write(f"Downloading audio to {tmp_dir} ...")
            try:
                get_audio_files_from_ytdl(url, tmp_dir)
            except RuntimeError as e:
                raise CommandError(str(e))

            # Process each album subdirectory
            for item in tmp_dir.iterdir():
                if not item.is_dir():
                    continue

                # Get album art
                self.stdout.write("Extracting album art...")
                cover = get_albumart_from_ytdl(url, item)
                if cover:
                    self.stdout.write(f"  Saved cover art: {cover.name}")
                else:
                    self.stdout.write("  No cover art extracted")

                # Remove any stray image files (keep only folder.jpg and mp3s)
                for f in item.iterdir():
                    if f.suffix.lower() in (".jpg", ".png", ".webp") and f.name != "folder.jpg":
                        f.unlink()
                        self.stdout.write(f"  Removed {f.name}")

                dest = library_dir / item.name
                if dest.exists():
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

        # Tag newly created tracks with the source URL
        if stats["created"] > 0:
            tagged = Track.objects.filter(source_url="").filter(
                file_path__startswith=str(library_dir),
            ).update(source_url=url)
            self.stdout.write(f"  Tagged {tagged} tracks with source URL")

        self.stdout.write(self.style.SUCCESS("Done."))
