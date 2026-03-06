from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from django.core.management.base import BaseCommand


SERVICE_NAME = "radioserver.service"
SYSTEMD_DIR = Path("/etc/systemd/system")

SERVICE_TEMPLATE = """\
[Unit]
Description=RadioServer
After=network.target

[Service]
User={user}
WorkingDirectory={working_dir}
ExecStart={python} -m gunicorn radioserver.wsgi:application --bind 0.0.0.0:9437 --workers 2 --timeout 120 --access-logfile - --access-logformat '%%(h)s %%(l)s %%(u)s %%(t)s "%%(r)s" %%(s)s %%(b)s "%%(f)s" "%%(a)s" cl=%%({{Content-Length}}o)s'
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


class Command(BaseCommand):
    help = "Install the radioserver systemd service for automatic startup."

    def handle(self, *args, **options):
        import radioserver.settings as settings_mod
        working_dir = Path(settings_mod.__file__).resolve().parent.parent
        python = sys.executable

        # Determine the user who owns the working directory
        import os
        import pwd
        stat = os.stat(working_dir)
        user = pwd.getpwuid(stat.st_uid).pw_name

        service_content = SERVICE_TEMPLATE.format(
            user=user,
            working_dir=working_dir,
            python=python,
        )

        dest = SYSTEMD_DIR / SERVICE_NAME

        try:
            dest.write_text(service_content)
        except PermissionError:
            self.stderr.write(
                "Permission denied. Run with sudo:\n"
                "  sudo ~/.local/bin/radioserver install_service"
            )
            return

        # Collect static files
        from django.core.management import call_command
        call_command("collectstatic", "--noinput")

        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", SERVICE_NAME], check=True)
        subprocess.run(["systemctl", "start", SERVICE_NAME], check=True)

        self.stdout.write(
            f"Installed and started {SERVICE_NAME}.\n"
            f"  Python: {python}\n"
            f"  Working dir: {working_dir}\n"
            f"  User: {user}\n"
            f"  Check status: systemctl status radioserver"
        )
