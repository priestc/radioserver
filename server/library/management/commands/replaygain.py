from __future__ import annotations

import json
import subprocess
from multiprocessing import Pool
from pathlib import Path

from django.core.management.base import BaseCommand

import mutagen
from mutagen import File as MutagenFile
from mutagen.id3 import TXXX


# EBU R128 target loudness
TARGET_LUFS = -18.0
TARGET_TP = -1.0  # true peak ceiling in dBTP


def _analyze_loudness(path: str) -> dict | None:
    """Use ffmpeg loudnorm filter to measure integrated loudness and true peak."""
    cmd = [
        "ffmpeg", "-hide_banner", "-i", path,
        "-af", "loudnorm=print_format=json",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return None

    # The loudnorm JSON is printed to stderr
    stderr = result.stderr
    # Find the JSON block in the output
    start = stderr.rfind("{")
    end = stderr.rfind("}") + 1
    if start < 0 or end <= start:
        return None

    try:
        data = json.loads(stderr[start:end])
        return {
            "input_i": float(data["input_i"]),
            "input_tp": float(data["input_tp"]),
        }
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def _compute_gain(input_lufs: float) -> float:
    """Compute the gain adjustment in dB to reach target loudness."""
    return TARGET_LUFS - input_lufs


def _write_replaygain_tags(path: str, gain_db: float, peak: float) -> bool:
    """Write ReplayGain tags to the audio file using mutagen."""
    gain_str = f"{gain_db:+.2f} dB"
    # Convert dBTP to linear peak
    peak_linear = 10 ** (peak / 20.0)
    peak_str = f"{peak_linear:.6f}"

    try:
        audio = MutagenFile(path)
    except mutagen.MutagenError:
        return False
    if audio is None:
        return False

    type_name = type(audio).__name__

    if type_name == "MP3":
        if audio.tags is None:
            audio.add_tags()
        audio.tags.add(TXXX(encoding=3, desc="replaygain_track_gain", text=[gain_str]))
        audio.tags.add(TXXX(encoding=3, desc="replaygain_track_peak", text=[peak_str]))
    elif type_name == "FLAC":
        audio["replaygain_track_gain"] = gain_str
        audio["replaygain_track_peak"] = peak_str
    elif type_name in ("OggVorbis", "OggOpus"):
        audio["replaygain_track_gain"] = [gain_str]
        audio["replaygain_track_peak"] = [peak_str]
    elif type_name == "MP4":
        # iTunes-style freeform atoms
        audio["----:com.apple.iTunes:replaygain_track_gain"] = \
            [mutagen.mp4.MP4FreeForm(gain_str.encode("utf-8"))]
        audio["----:com.apple.iTunes:replaygain_track_peak"] = \
            [mutagen.mp4.MP4FreeForm(peak_str.encode("utf-8"))]
    else:
        # For any other ID3-based format, try TXXX frames directly
        tags = getattr(audio, "tags", None)
        if tags is not None and hasattr(tags, "add"):
            try:
                tags.add(TXXX(encoding=3, desc="replaygain_track_gain", text=[gain_str]))
                tags.add(TXXX(encoding=3, desc="replaygain_track_peak", text=[peak_str]))
            except Exception:
                return False
        else:
            return False

    try:
        audio.save()
        return True
    except mutagen.MutagenError:
        return False


def _has_replaygain(path: str) -> bool:
    """Check if a file already has ReplayGain tags."""
    try:
        audio = MutagenFile(path)
    except mutagen.MutagenError:
        return False
    if audio is None:
        return False

    type_name = type(audio).__name__

    if type_name == "MP3":
        if audio.tags is None:
            return False
        for frame in audio.tags.getall("TXXX"):
            if frame.desc.lower() == "replaygain_track_gain":
                return True
        return False
    elif type_name == "FLAC":
        return "replaygain_track_gain" in audio
    elif type_name in ("OggVorbis", "OggOpus"):
        return "replaygain_track_gain" in audio
    elif type_name == "MP4":
        return "----:com.apple.iTunes:replaygain_track_gain" in audio
    return False


def _process_track(args: tuple) -> tuple[str, str, str]:
    """Process a single track. Returns (path, status, detail).

    Designed to run in a worker process — no Django ORM access.
    """
    path, force = args

    if not Path(path).is_file():
        return (path, "missing", "")

    if not force and _has_replaygain(path):
        return (path, "skipped", "")

    loudness = _analyze_loudness(path)
    if loudness is None:
        return (path, "error", "could not analyze")

    gain_db = _compute_gain(loudness["input_i"])
    peak = loudness["input_tp"]

    if _write_replaygain_tags(path, gain_db, peak):
        return (path, "tagged", f"gain={gain_db:+.2f} dB, peak={peak:.2f} dBTP")
    else:
        return (path, "error", "failed to write tags")


class Command(BaseCommand):
    help = "Analyze tracks and write ReplayGain tags for volume normalization."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-analyze tracks that already have ReplayGain tags.",
        )
        parser.add_argument(
            "--album",
            type=int,
            help="Only process tracks from this album ID.",
        )
        parser.add_argument(
            "--cores",
            type=int,
            default=1,
            help="Number of parallel processes (default: 1).",
        )

    def handle(self, **options):
        from library.models import Track

        # Check ffmpeg is available
        try:
            subprocess.run(
                ["ffmpeg", "-version"], capture_output=True, check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            self.stderr.write(self.style.ERROR(
                "ffmpeg is required but not found. Install it first."
            ))
            return

        qs = Track.objects.all()
        if options["album"]:
            qs = qs.filter(album_id=options["album"])

        paths = list(qs.order_by("id").values_list("file_path", flat=True))
        total = len(paths)
        cores = max(1, options["cores"])
        force = options["force"]

        self.stdout.write(f"Processing {total} tracks with {cores} core(s)...\n")

        tagged = 0
        skipped = 0
        errors = 0

        work = [(p, force) for p in paths]

        if cores == 1:
            results = (_process_track(w) for w in work)
        else:
            pool = Pool(processes=cores)
            results = pool.imap_unordered(_process_track, work)

        for i, (path, status, detail) in enumerate(results, 1):
            if status == "tagged":
                self.stdout.write(f"  [{i}/{total}] {Path(path).name}: {detail}")
                tagged += 1
            elif status == "skipped":
                skipped += 1
            elif status == "missing":
                self.stdout.write(f"  [{i}/{total}] MISSING: {path}")
                errors += 1
            elif status == "error":
                self.stdout.write(self.style.WARNING(f"  [{i}/{total}] {Path(path).name}: {detail}"))
                errors += 1

        if cores > 1:
            pool.close()
            pool.join()

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Done. Tagged: {tagged}, Skipped: {skipped}, Errors: {errors}"
        ))
