from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from library.ai import get_backend, lookup_year
from library.models import Album
from library.tags import write_track_year


class Command(BaseCommand):
    help = "Use AI to look up release years for tracks in an album."

    def add_arguments(self, parser):
        parser.add_argument("album_id", type=int, help="ID of the album to process.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print results without saving to the database.",
        )
        parser.add_argument(
            "--backend",
            choices=["openai", "claude", "google", "deepseek", "groq"],
            default="claude",
            help="AI backend to use (default: claude).",
        )

    def handle(self, **options):
        backend = options["backend"]

        try:
            ask = get_backend(backend)
        except ValueError as e:
            raise CommandError(str(e))

        album_id = options["album_id"]
        dry_run = options["dry_run"]

        try:
            album = Album.objects.get(id=album_id)
        except Album.DoesNotExist:
            raise CommandError(f"Album with id {album_id} does not exist.")

        self.stdout.write(f"Album: {album.title} (id={album.id})")
        self.stdout.write(f"Backend: {backend}")
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no database changes"))

        tracks = album.tracks.all()

        updated = 0
        failed = 0

        for track in tracks:
            artist = track.artists.first()
            artist_name = artist.name if artist else "Unknown Artist"

            try:
                year = lookup_year(ask, track.title, artist_name)
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(
                        f"  FAIL: \"{track.title}\" by {artist_name} — {e}"
                    )
                )
                failed += 1
                continue

            if year is None:
                self.stdout.write(
                    self.style.ERROR(
                        f"  FAIL: \"{track.title}\" by {artist_name} — "
                        f"could not parse year from response"
                    )
                )
                failed += 1
                continue

            if dry_run:
                self.stdout.write(f"  \"{track.title}\" by {artist_name} → {year}")
            else:
                track.year = year
                track.save(update_fields=["year"])
                write_track_year(track)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  \"{track.title}\" by {artist_name} → {year}"
                    )
                )
            updated += 1

        self.stdout.write("")
        self.stdout.write(f"Done. Updated: {updated}, Failed: {failed}")
