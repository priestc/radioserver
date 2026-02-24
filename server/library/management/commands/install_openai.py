from __future__ import annotations

import configparser
from pathlib import Path

from django.core.management.base import BaseCommand

CONF_PATH = Path.home() / ".radioserver.conf"


class Command(BaseCommand):
    help = "Save your OpenAI API key to ~/.radioserver.conf"

    def add_arguments(self, parser):
        parser.add_argument("api_key", help="Your OpenAI API key.")

    def handle(self, **options):
        config = configparser.ConfigParser()
        config.read(CONF_PATH)

        if not config.has_section("api"):
            config.add_section("api")

        config.set("api", "openai_key", options["api_key"])

        with open(CONF_PATH, "w") as f:
            config.write(f)

        self.stdout.write(self.style.SUCCESS(f"OpenAI API key saved to {CONF_PATH}"))
