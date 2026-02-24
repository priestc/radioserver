from __future__ import annotations

import re

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from library.models import Album


def _ask_openai(prompt: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=20,
    )
    return response.choices[0].message.content.strip()


def _ask_claude(prompt: str) -> str:
    from anthropic import Anthropic

    client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


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
            choices=["openai", "claude"],
            default="claude",
            help="AI backend to use (default: claude).",
        )

    def handle(self, **options):
        backend = options["backend"]

        if backend == "openai":
            if not settings.OPENAI_API_KEY:
                raise CommandError(
                    "OPENAI_API_KEY not set. Add openai_key under [api] in ~/.radioserver.conf"
                )
            ask = _ask_openai
        else:
            if not settings.ANTHROPIC_API_KEY:
                raise CommandError(
                    "ANTHROPIC_API_KEY not set. Add anthropic_key under [api] in ~/.radioserver.conf"
                )
            ask = _ask_claude

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

            prompt = (
                f"What year was the song '{track.title}' by {artist_name} "
                f"originally released? Reply with just the 4-digit year."
            )

            try:
                answer = ask(prompt)
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
                    self.stdout.write(f"  \"{track.title}\" by {artist_name} → {year}")
                else:
                    track.year = year
                    track.save(update_fields=["year"])
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  \"{track.title}\" by {artist_name} → {year}"
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
