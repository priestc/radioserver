from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from django.core.management.base import BaseCommand


SERVICE_NAME = "radioserver.service"
SYSTEMD_DIR = Path("/etc/systemd/system")


class Command(BaseCommand):
    help = "Install the radioserver systemd service for automatic startup."

    def handle(self, *args, **options):
        # Find the .service file bundled with the package
        source = Path(__file__).resolve().parents[4] / SERVICE_NAME
        if not source.is_file():
            self.stderr.write(f"Service file not found at {source}")
            return

        dest = SYSTEMD_DIR / SERVICE_NAME

        # Update the service file with the current Python and working directory
        python_path = shutil.which("python3") or sys.executable
        working_dir = source.parent / "server"
        service_content = source.read_text()
        service_content = service_content.replace(
            "/usr/bin/python3", python_path
        ).replace(
            "/home/chris/radioserver/server", str(working_dir)
        )

        # Write to systemd directory (requires root)
        try:
            dest.write_text(service_content)
        except PermissionError:
            self.stderr.write(
                "Permission denied. Run with sudo:\n"
                f"  sudo radioserver install_service"
            )
            return

        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", SERVICE_NAME], check=True)
        subprocess.run(["systemctl", "start", SERVICE_NAME], check=True)

        self.stdout.write(
            f"Installed and started {SERVICE_NAME}.\n"
            f"  Python: {python_path}\n"
            f"  Working dir: {working_dir}\n"
            f"  Check status: systemctl status radioserver"
        )
