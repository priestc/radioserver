from __future__ import annotations

import re
import time

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


def _ask_deepseek(prompt: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=settings.DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=20,
    )
    return response.choices[0].message.content.strip()


def _ask_groq(prompt: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=settings.GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=20,
    )
    return response.choices[0].message.content.strip()


def _ask_google(prompt: str) -> str:
    from google import genai

    client = genai.Client(api_key=settings.GOOGLE_AI_API_KEY)
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=prompt,
    )
    return response.text.strip()


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

        if backend == "openai":
            if not settings.OPENAI_API_KEY:
                raise CommandError(
                    "OPENAI_API_KEY not set. Add openai_key under [api] in ~/.radioserver.conf"
                )
            ask = _ask_openai
        elif backend == "google":
            if not settings.GOOGLE_AI_API_KEY:
                raise CommandError(
                    "GOOGLE_AI_API_KEY not set. Add google_ai_key under [api] in ~/.radioserver.conf"
                )
            ask = _ask_google
        elif backend == "deepseek":
            if not settings.DEEPSEEK_API_KEY:
                raise CommandError(
                    "DEEPSEEK_API_KEY not set. Add deepseek_key under [api] in ~/.radioserver.conf"
                )
            ask = _ask_deepseek
        elif backend == "groq":
            if not settings.GROQ_API_KEY:
                raise CommandError(
                    "GROQ_API_KEY not set. Add groq_key under [api] in ~/.radioserver.conf"
                )
            ask = _ask_groq
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

            answer = None
            for attempt in range(5):
                try:
                    answer = ask(prompt)
                    break
                except Exception as e:
                    error_str = str(e)
                    if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                        wait = 10 * (attempt + 1)
                        self.stdout.write(
                            self.style.WARNING(
                                f"  Rate limited, waiting {wait}s..."
                            )
                        )
                        time.sleep(wait)
                    else:
                        self.stdout.write(
                            self.style.ERROR(
                                f"  FAIL: \"{track.title}\" by {artist_name} — {e}"
                            )
                        )
                        failed += 1
                        break
            else:
                self.stdout.write(
                    self.style.ERROR(
                        f"  FAIL: \"{track.title}\" by {artist_name} — "
                        f"still rate limited after 5 retries"
                    )
                )
                failed += 1
                continue

            if answer is None:
                continue

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

        self.stdout.write("")
        self.stdout.write(f"Done. Updated: {updated}, Failed: {failed}")
