from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from library.models import SplitJob
from library.splitter import SplitError, validate_tracks

FIELDS = ["duration", "album_title", "album_artist", "dest_folder", "year", "genre",
          "playlist", "tracks", "segments", "review_notes"]


class Command(BaseCommand):
    help = (
        "Create or update SplitJobs from JSON files. Each file holds one job: "
        '{"source_path", "duration", "album_title", "album_artist", "dest_folder", '
        '"year", "genre", "playlist", "tracks", "segments", "review_notes"}.'
    )

    def add_arguments(self, parser):
        parser.add_argument("files", nargs="+", help="Job JSON files")
        parser.add_argument(
            "--force", action="store_true",
            help="Overwrite jobs that already exist (including tracks you've edited).",
        )

    def handle(self, files, force, **options):
        created = updated = skipped = 0
        for f in files:
            try:
                data = json.loads(Path(f).read_text())
            except (OSError, json.JSONDecodeError) as e:
                raise CommandError(f"{f}: {e}")
            if not Path(data.get("source_path", "")).is_file():
                raise CommandError(f"{f}: source_path {data.get('source_path')!r} is not a file")
            try:
                data["tracks"] = validate_tracks(data.get("tracks", []), float(data["duration"]))
            except (SplitError, KeyError, ValueError) as e:
                raise CommandError(f"{f}: {e}")
            job = SplitJob.objects.filter(source_path=data["source_path"]).first()
            if job and not force:
                skipped += 1
                self.stdout.write(f"skip (exists): {job.album_title}")
                continue
            job = job or SplitJob(source_path=data["source_path"])
            is_new = job.pk is None
            for field in FIELDS:
                if field in data:
                    setattr(job, field, data[field])
            job.waveform = []
            job.save()
            created += is_new
            updated += not is_new
            self.stdout.write(f"{'created' if is_new else 'updated'}: {job.album_title}")
        self.stdout.write(self.style.SUCCESS(f"{created} created, {updated} updated, {skipped} skipped"))
