from __future__ import annotations

import os
from pathlib import Path

from django.core.management.base import BaseCommand

ALIAS_LINE = (
    'alias radioserver-update='
    "'pipx install --force git+https://github.com/priestc/radioserver.git"
    " && sudo systemctl restart radioserver'"
)


class Command(BaseCommand):
    help = "Install a 'radioserver-update' alias into ~/.bashrc."

    def handle(self, **options):
        bashrc = Path.home() / ".bashrc"

        if bashrc.exists() and ALIAS_LINE in bashrc.read_text():
            self.stdout.write("Alias already installed in ~/.bashrc")
        else:
            with open(bashrc, "a") as f:
                f.write(f"\n{ALIAS_LINE}\n")
            self.stdout.write(self.style.SUCCESS(f"Added alias to {bashrc}"))

        self.stdout.write("")
        self.stdout.write("Run the following to activate it now:")
        self.stdout.write("  source ~/.bashrc")
        self.stdout.write("")
        self.stdout.write("Then update anytime with:")
        self.stdout.write("  radioserver-update")
