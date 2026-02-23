from __future__ import annotations

from django.core.management.base import BaseCommand

from library.models import Album, Artist, PlaylistItem, Track


class Command(BaseCommand):
    help = "Delete all library data from the database. Files on disk are not touched. Genre groups are preserved."

    def handle(self, **options):
        self.stdout.write(self.style.WARNING("This will delete ALL library data from the database (genre groups preserved)."))
        self.stdout.write(f"  Tracks:         {Track.objects.count()}")
        self.stdout.write(f"  Albums:         {Album.objects.count()}")
        self.stdout.write(f"  Artists:        {Artist.objects.count()}")
        self.stdout.write(f"  Playlist Items: {PlaylistItem.objects.count()}")
        self.stdout.write("")

        confirm = input("Type 'yes' to confirm: ").strip().lower()
        if confirm != "yes":
            self.stdout.write(self.style.WARNING("Aborted."))
            return

        PlaylistItem.objects.all().delete()
        Track.objects.all().delete()
        Album.objects.all().delete()
        Artist.objects.all().delete()

        self.stdout.write(self.style.SUCCESS("All library data deleted. Genre groups and files on disk were not touched."))
