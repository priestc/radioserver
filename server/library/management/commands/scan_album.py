from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from library.models import Album, Artist, Track, TrackArtist
from library.tags import read_tags


class Command(BaseCommand):
    help = "Scan a single album folder, printing what it finds and updating the DB."

    def add_arguments(self, parser):
        parser.add_argument(
            "path",
            help="Path to the album folder (absolute, or relative to the library root).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Only print what would happen, don't touch the database.",
        )

    def handle(self, **options):
        library_path = settings.MUSIC_LIBRARY_PATH
        extensions = settings.MUSIC_EXTENSIONS
        dry_run = options["dry_run"]

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
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no database changes"))
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

            if dry_run:
                self.stdout.write("  DB operations that would occur:")
                self.stdout.write(f"    Artist.get_or_create(name={file_album_artist!r})  [album artist]")
                self.stdout.write(f"    Album.get_or_create(title={album_title!r}, artist=<{file_album_artist!r}>)")
                for i, name in enumerate(track_artists):
                    self.stdout.write(f"    Artist.get_or_create(name={name!r})  [track artist]")
                    self.stdout.write(f"    TrackArtist(track=..., artist=<{name!r}>, position={i})")
            else:
                # Album artist
                aa_obj, aa_created = Artist.objects.get_or_create(name=file_album_artist)
                self._log_get_or_create("Artist", aa_obj.name, aa_created, suffix="[album artist]")

                # Album
                album_obj, album_created = Album.objects.get_or_create(
                    title=album_title, artist=aa_obj,
                )
                if album_created or album_obj.year is None:
                    album_obj.year = tag_data.get("year")
                    album_obj.total_tracks = tag_data.get("total_tracks")
                    album_obj.total_discs = tag_data.get("total_discs")
                    album_obj.save()
                self._log_get_or_create("Album", f"{aa_obj.name} — {album_title}", album_created)

                # Track
                defaults = {
                    "title": tag_data["title"],
                    "album": album_obj,
                    "track_number": tag_data["track_number"],
                    "disc_number": tag_data["disc_number"],
                    "genre": tag_data["genre"],
                    "year": tag_data["year"],
                    "duration": tag_data["duration"],
                    "bitrate": tag_data["bitrate"],
                    "sample_rate": tag_data["sample_rate"],
                    "channels": tag_data["channels"],
                    "format": tag_data["format"],
                    "file_size": tag_data["file_size"],
                    "file_mtime": tag_data["file_mtime"],
                }
                track_obj, track_created = Track.objects.update_or_create(
                    file_path=tag_data["file_path"], defaults=defaults,
                )
                if track_created:
                    track_obj.source = "local filesystem"
                    track_obj.save(update_fields=["source"])
                self._log_get_or_create("Track", tag_data["title"], track_created)

                # Track artists M2M
                TrackArtist.objects.filter(track=track_obj).delete()
                for i, name in enumerate(track_artists):
                    artist_obj, artist_created = Artist.objects.get_or_create(name=name)
                    self._log_get_or_create("Artist", name, artist_created, suffix="[track artist]")
                    TrackArtist.objects.create(track=track_obj, artist=artist_obj, position=i)
                    self.stdout.write(f"    + TrackArtist(artist={name!r}, position={i})")

            self.stdout.write("")

        if len(album_title_seen) > 1:
            self.stdout.write(self.style.WARNING(
                f"WARNING: Multiple album titles found in tags: {album_title_seen}"
            ))

        if not dry_run:
            self.stdout.write(self.style.SUCCESS(f"Done. Processed {len(files)} files."))

    def _log_get_or_create(self, model, label, created, suffix=""):
        verb = "CREATED" if created else "exists"
        style = self.style.SUCCESS if created else self.style.HTTP_NOT_MODIFIED
        line = f"    {model}: {label!r} — {verb}"
        if suffix:
            line += f"  {suffix}"
        self.stdout.write(style(line))
