from __future__ import annotations

import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from library.scanner import scan


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

        out_dir = Path(settings.MUSIC_LIBRARY_PATH) / "from youtube music"
        out_dir.mkdir(parents=True, exist_ok=True)

        # Snapshot existing album dirs so we can detect new ones
        dirs_before = set(out_dir.iterdir())

        output_template = str(out_dir / "%(album,playlist_title)s/%(track_number)02d %(title)s.%(ext)s")

        cmd = [
            "yt-dlp",
            "-x", "--audio-format", "mp3",
            "-f", "bestaudio[abr<=192]/bestaudio",
            "--embed-thumbnail",
            "--add-metadata",
            "--parse-metadata", "playlist_index:%(track_number)s",
            "--write-thumbnail",
            "--convert-thumbnails", "jpg",
            "--yes-playlist",
            "-o", output_template,
            url,
        ]

        self.stdout.write(f"Downloading to {out_dir} ...")
        self.stdout.write(f"Running: {' '.join(cmd)}\n")

        result = subprocess.run(cmd)
        if result.returncode != 0:
            raise CommandError(f"yt-dlp exited with code {result.returncode}")

        # Find new album directories
        dirs_after = set(out_dir.iterdir())
        new_dirs = dirs_after - dirs_before

        # Rename thumbnail files to folder.jpg in each album directory
        for album_dir in (new_dirs or dirs_after):
            if not album_dir.is_dir():
                continue
            for thumb in album_dir.glob("*.jpg"):
                if thumb.name != "folder.jpg":
                    thumb.rename(album_dir / "folder.jpg")
                    self.stdout.write(f"  Renamed {thumb.name} -> folder.jpg in {album_dir.name}/")
                    break

        # Scan the library to import new tracks
        self.stdout.write("\nScanning library...")
        stats = scan()
        self.stdout.write(f"  Created: {stats['created']}")
        self.stdout.write(f"  Updated: {stats['updated']}")
        self.stdout.write(self.style.SUCCESS("Done."))
