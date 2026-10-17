from __future__ import annotations

import configparser
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

CONF_PATH = Path.home() / ".radioserver.conf"


class Command(BaseCommand):
    help = ("Save Discogs credentials to ~/.radioserver.conf: a personal access token "
            "(discogs.com/settings/developers, 'Generate new token'), or an app's consumer key and secret.")

    def add_arguments(self, parser):
        parser.add_argument("token", nargs="?", help="Personal access token.")
        parser.add_argument("--key", help="App consumer key (use with --secret instead of a token).")
        parser.add_argument("--secret", help="App consumer secret.")

    def handle(self, **options):
        token, key, secret = options["token"], options["key"], options["secret"]
        if not token and not (key and secret):
            raise CommandError("Give a personal token, or both --key and --secret.")

        config = configparser.ConfigParser()
        config.read(CONF_PATH)
        if not config.has_section("api"):
            config.add_section("api")
        if token:
            config.set("api", "discogs_token", token)
        else:
            config.set("api", "discogs_key", key)
            config.set("api", "discogs_secret", secret)
        with open(CONF_PATH, "w") as f:
            config.write(f)
        CONF_PATH.chmod(0o600)

        self.stdout.write(self.style.SUCCESS(f"Discogs credentials saved to {CONF_PATH}. Restart radioserver to use them."))
