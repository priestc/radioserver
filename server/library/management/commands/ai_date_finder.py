from __future__ import annotations

import re

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from openai import OpenAI

from library.models import Album


class Command(BaseCommand):
    help = "Use OpenAI to look up release years for tracks in an album."

    def add_arguments(self, parser):
        parser.add_argument("album_id", type=int, help="ID of the album to process.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print results without saving to the database.",
        )

    def handle(self, **options):
        api_key = settings.OPENAI_API_KEY
        if not api_key:
            raise CommandError(
                "OPENAI_API_KEY not set. Add openai_key under [api] in ~/.radioserver.conf"
            )

        album_id = options["album_id"]
        dry_run = options["dry_run"]

        try:
            album = Album.objects.get(id=album_id)
        except Album.DoesNotExist:
            raise CommandError(f"Album with id {album_id} does not exist.")

        self.stdout.write(f"Album: {album.title} (id={album.id})")
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no database changes"))

        client = OpenAI(api_key=api_key)
        tracks = album.tracks.all()

        updated = 0
        failed = 0

        for track in tracks:
            artist = track.artists.first()
            artist_name = artist.name if artist else "Unknown Artist"

            prompt = (
                f"What year was the song '{track.title}' by {artist_name} "
                f"originally released? Reply with just the 4-digit year."
            )

            try:
                response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=20,
                )
                answer = response.choices[0].message.content.strip()
                match = re.search(r"\b(19\d{2}|20\d{2})\b", answer)

                if not match:
                    self.stdout.write(
                        self.style.ERROR(
                            f"  FAIL: \"{track.title}\" by {artist_name} — "
                            f"could not parse year from: {answer!r}"
                        )
                    )
                    failed += 1
                    continue

                year = int(match.group(1))

                if dry_run:
                    self.stdout.write(f"  \"{track.title}\" by {artist_name} \u2192 {year}")
                else:
                    track.year = year
                    track.save(update_fields=["year"])
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  \"{track.title}\" by {artist_name} \u2192 {year}"
                        )
                    )
                updated += 1

            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(
                        f"  FAIL: \"{track.title}\" by {artist_name} — {e}"
                    )
                )
                failed += 1

        self.stdout.write("")
        self.stdout.write(f"Done. Updated: {updated}, Failed: {failed}")
