from __future__ import annotations

import subprocess
import sys

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Update radioserver from GitHub and restart the service."

    def handle(self, **options):
        self.stdout.write("Updating radioserver via pipx...")
        subprocess.run(
            [sys.executable, "-m", "pipx", "install", "--force",
             "git+https://github.com/priestc/radioserver.git"],
            check=True,
        )

        self.stdout.write("Restarting radioserver service...")
        subprocess.run(["sudo", "systemctl", "restart", "radioserver"], check=True)

        self.stdout.write(self.style.SUCCESS("Done."))
