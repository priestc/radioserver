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

        if stats.get("updated_files"):
            self.stdout.write("")
            self.stdout.write(self.style.WARNING("Updated files:"))
            for path, fields in stats["updated_files"]:
                self.stdout.write(f"  {path}")
                self.stdout.write(f"    Changed: {', '.join(fields)}")

        if stats.get("error_files"):
            self.stdout.write("")
            self.stdout.write(self.style.WARNING("Files with errors:"))
            for path in stats["error_files"]:
                self.stdout.write(f"  {path}")

        if stats.get("cover_invalid_albums"):
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                f"Removed invalid cover art from {stats['cover_invalid']} albums:"
            ))
            for album_name in stats["cover_invalid_albums"]:
                self.stdout.write(f"  {album_name}")

        if options["clean"]:
            self.stdout.write(f"  Cleaned tracks:  {stats.get('cleaned_tracks', 0)}")
            self.stdout.write(f"  Cleaned albums:  {stats.get('cleaned_albums', 0)}")
            self.stdout.write(f"  Cleaned artists: {stats.get('cleaned_artists', 0)}")

        self.stdout.write(self.style.SUCCESS("Done."))
