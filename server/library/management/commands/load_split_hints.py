from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from library.models import SplitJob


class Command(BaseCommand):
    help = (
        "Load the splitter editor's helpers (transcript sentences, suggested boundaries, noise bursts) "
        "from <job id>.json files. Only the job's hints change; its tracks are never touched."
    )

    def add_arguments(self, parser):
        parser.add_argument("files", nargs="+", help="Hint files named <job id>.json")

    def handle(self, files, **options):
        for f in files:
            path = Path(f)
            try:
                job = SplitJob.objects.get(pk=int(path.stem))
            except (ValueError, SplitJob.DoesNotExist):
                raise CommandError(f"{f}: no SplitJob with id {path.stem!r}")
            try:
                hints = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as e:
                raise CommandError(f"{f}: {e}")
            if not isinstance(hints, dict):
                raise CommandError(f"{f}: expected a JSON object")
            sugg = hints.get("suggestions")
            if sugg is not None and len(sugg) != len(job.tracks):
                raise CommandError(f"{f}: {len(sugg)} suggestions for {len(job.tracks)} tracks")
            job.hints = hints
            job.save(update_fields=["hints"])
            self.stdout.write(f"{job.id} {job.album_title}: {len(hints.get('sentences', []))} sentences, "
                              f"{len(sugg or [])} suggestions, {len(hints.get('crinkles', []))} noise bursts")
