from __future__ import annotations

from django.core.management.base import BaseCommand

from library.scanner import scan


class Command(BaseCommand):
    help = "Scan the music library and index tracks into the database."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-read tags for all files, even if mtime hasn't changed.",
        )
        parser.add_argument(
            "--clean",
            action="store_true",
            help="Remove database entries for files that no longer exist on disk.",
        )

    def handle(self, **options):
        self.stdout.write("Scanning music library...")
        stats = scan(force=options["force"], clean=options["clean"])

        self.stdout.write(f"  Scanned:  {stats['scanned']}")
        self.stdout.write(f"  Created:  {stats['created']}")
        self.stdout.write(f"  Updated:  {stats['updated']}")
        self.stdout.write(f"  Skipped:  {stats['skipped']}")
        self.stdout.write(f"  Errors:   {stats['errors']}")

        if options["clean"]:
            self.stdout.write(f"  Cleaned tracks:  {stats.get('cleaned_tracks', 0)}")
            self.stdout.write(f"  Cleaned albums:  {stats.get('cleaned_albums', 0)}")
            self.stdout.write(f"  Cleaned artists: {stats.get('cleaned_artists', 0)}")

        self.stdout.write(self.style.SUCCESS("Done."))
