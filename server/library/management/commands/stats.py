from __future__ import annotations

import re
import subprocess

from django.core.management.base import BaseCommand

# Matches gunicorn combined access log:
# 192.168.1.50 - - [01/Mar/2026:12:00:00 +0000] "GET /path HTTP/1.1" 200 12345 "ref" "ua"
LOG_RE = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ \[.*?\] "(?P<method>\S+) (?P<path>\S+) \S+" (?P<status>\d+) (?P<bytes>\S+)'
)

# Replace numeric path segments with * for grouping
NUMERIC_SEGMENT_RE = re.compile(r"(?<=/)\d+(?=/)")


def humanize_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


def normalize_path(path: str) -> str:
    return NUMERIC_SEGMENT_RE.sub("*", path)


class Command(BaseCommand):
    help = "Show bandwidth stats from radioserver access logs."

    def add_arguments(self, parser):
        parser.add_argument(
            "since",
            help='Time window passed to journalctl --since, e.g. "3 days ago".',
        )

    def handle(self, *args, **options):
        since = options["since"]

        result = subprocess.run(
            [
                "journalctl",
                "-u", "radioserver",
                "--since", since,
                "--no-pager",
                "-o", "cat",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            self.stderr.write(f"journalctl failed: {result.stderr.strip()}")
            return

        ip_stats: dict[str, list[int]] = {}       # ip -> [bytes, count]
        endpoint_stats: dict[str, list[int]] = {}  # pattern -> [bytes, count]
        grand_bytes = 0
        grand_count = 0

        for line in result.stdout.splitlines():
            m = LOG_RE.match(line)
            if not m:
                continue

            resp_bytes_str = m.group("bytes")
            if resp_bytes_str == "-":
                resp_bytes = 0
            else:
                resp_bytes = int(resp_bytes_str)

            ip = m.group("ip")
            path = normalize_path(m.group("path"))

            entry = ip_stats.get(ip)
            if entry:
                entry[0] += resp_bytes
                entry[1] += 1
            else:
                ip_stats[ip] = [resp_bytes, 1]

            entry = endpoint_stats.get(path)
            if entry:
                entry[0] += resp_bytes
                entry[1] += 1
            else:
                endpoint_stats[path] = [resp_bytes, 1]

            grand_bytes += resp_bytes
            grand_count += 1

        if not grand_count:
            self.stdout.write("No access log entries found.")
            return

        # Bandwidth by IP
        self.stdout.write("\n=== Bandwidth by IP ===\n")
        sorted_ips = sorted(ip_stats.items(), key=lambda x: x[1][0], reverse=True)
        ip_width = max(len(ip) for ip, _ in sorted_ips)
        for ip, (total, count) in sorted_ips:
            self.stdout.write(
                f"  {ip:<{ip_width}}  {humanize_bytes(total):>12}  {count:>6} requests"
            )

        # Bandwidth by endpoint
        self.stdout.write("\n=== Bandwidth by endpoint ===\n")
        sorted_eps = sorted(endpoint_stats.items(), key=lambda x: x[1][0], reverse=True)
        ep_width = max(len(ep) for ep, _ in sorted_eps)
        for ep, (total, count) in sorted_eps:
            self.stdout.write(
                f"  {ep:<{ep_width}}  {humanize_bytes(total):>12}  {count:>6} requests"
            )

        self.stdout.write(
            f"\nTotal: {humanize_bytes(grand_bytes)}, {grand_count} requests\n"
        )
