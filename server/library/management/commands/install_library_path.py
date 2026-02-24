from __future__ import annotations

import configparser
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

CONF_PATH = Path.home() / ".radioserver.conf"


class Command(BaseCommand):
    help = "Save your music library path to ~/.radioserver.conf"

    def add_arguments(self, parser):
        parser.add_argument("path", help="Absolute path to your music library folder.")

    def handle(self, **options):
        library_path = Path(options["path"])
        if not library_path.is_absolute():
            raise CommandError("Please provide an absolute path.")
        if not library_path.is_dir():
            raise CommandError(f"Not a directory: {library_path}")

        config = configparser.ConfigParser()
        config.read(CONF_PATH)

        if not config.has_section("library"):
            config.add_section("library")

        config.set("library", "path", str(library_path))

        with open(CONF_PATH, "w") as f:
            config.write(f)

        self.stdout.write(self.style.SUCCESS(f"Library path saved to {CONF_PATH}"))
