from __future__ import annotations

import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from library.tags import read_tags


class Command(BaseCommand):
    help = "Dry-run scan of a single album folder. Shows what would be written to the DB without touching it."

    def add_arguments(self, parser):
        parser.add_argument(
            "path",
            help="Path to the album folder (absolute, or relative to the library root).",
        )

    def handle(self, **options):
        library_path = settings.MUSIC_LIBRARY_PATH
        extensions = settings.MUSIC_EXTENSIONS

        album_path = Path(options["path"])
        if not album_path.is_absolute():
            album_path = Path(library_path) / album_path
        if not album_path.is_dir():
            raise CommandError(f"Not a directory: {album_path}")

        # Derive album artist from folder position relative to library root
        try:
            rel = album_path.relative_to(library_path)
            album_artist_name = rel.parts[0] if rel.parts else "Unknown Artist"
        except ValueError:
            album_artist_name = "(outside library root)"

        self.stdout.write(f"Library root:  {library_path}")
        self.stdout.write(f"Album folder:  {album_path}")
        self.stdout.write(f"Relative path: {rel}")
        self.stdout.write(f"Album artist (from folder): {album_artist_name}")
        self.stdout.write("")

        files = sorted(
            f for f in album_path.iterdir()
            if f.is_file()
            and not f.name.startswith("._")
            and f.suffix.lstrip(".").lower() in extensions
        )

        if not files:
            self.stderr.write("No audio files found in this folder.")
            return

        album_title_seen = set()

        for filepath in files:
            self.stdout.write(self.style.MIGRATE_HEADING(f"--- {filepath.name} ---"))

            tag_data = read_tags(str(filepath))
            if tag_data is None:
                self.stdout.write(self.style.ERROR("  Could not read tags"))
                continue

            # Show what the scanner would derive
            rel_file = Path(filepath).relative_to(library_path)
            file_album_artist = rel_file.parts[0] if len(rel_file.parts) > 1 else "Unknown Artist"

            album_title = tag_data["album"]
            track_artists = tag_data["artists"]

            album_title_seen.add(album_title)

            self.stdout.write(f"  Album (from tag):           {album_title}")
            self.stdout.write(f"  Album artist (from folder): {file_album_artist}")
            self.stdout.write(f"  Track artists (from tag):   {track_artists}")
            self.stdout.write(f"  Title:                      {tag_data['title']}")
            self.stdout.write(f"  Track#: {tag_data['track_number']}  Disc#: {tag_data['disc_number']}")
            self.stdout.write(f"  Genre: {tag_data['genre']}  Year: {tag_data['year']}")
            self.stdout.write("")

            self.stdout.write(self.style.SUCCESS("  DB operations that would occur:"))
            self.stdout.write(f"    Artist.get_or_create(name={file_album_artist!r})  [album artist]")
            self.stdout.write(f"    Album.get_or_create(title={album_title!r}, artist=<{file_album_artist!r}>)")
            for i, name in enumerate(track_artists):
                self.stdout.write(f"    Artist.get_or_create(name={name!r})  [track artist]")
                self.stdout.write(f"    TrackArtist(track=..., artist=<{name!r}>, position={i})")
            self.stdout.write("")

        if len(album_title_seen) > 1:
            self.stdout.write(self.style.WARNING(
                f"WARNING: Multiple album titles found in tags: {album_title_seen}"
            ))
