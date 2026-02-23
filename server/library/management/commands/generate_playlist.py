from __future__ import annotations

from django.core.management.base import BaseCommand

from library.playlist import generate_playlist


class Command(BaseCommand):
    help = "Generate playlist items to fill the given number of hours."

    def add_arguments(self, parser):
        parser.add_argument(
            "hours",
            type=float,
            help="Target duration in hours.",
        )

    def handle(self, *args, **options):
        target_seconds = options["hours"] * 3600
        items_created, total_duration = generate_playlist(target_seconds)

        if items_created == 0:
            self.stderr.write("No eligible tracks found.")
            return

        hours = total_duration / 3600
        self.stdout.write(
            f"Created {items_created} playlist items ({hours:.1f} hours)."
        )
