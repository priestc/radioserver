"""Cut long recordings (SplitJob) into separately tagged library tracks."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import mutagen
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from library.models import SplitJob, Track
from library.scanner import _upsert_track
from library.tags import read_tags
from library.views import check_cover_status

WAVEFORM_SAMPLE_RATE = 8000


class SplitError(Exception):
    pass


def compute_waveform(job: SplitJob) -> list[int]:
    """Decode the source to mono PCM and return one peak (0-255) per resolution step."""
    import array

    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", job.source_path, "-ac", "1",
         "-ar", str(WAVEFORM_SAMPLE_RATE), "-f", "s16le", "-"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise SplitError(f"ffmpeg could not decode {job.source_path}: {proc.stderr.decode(errors='replace')[-500:]}")
    samples = array.array("h", proc.stdout[: len(proc.stdout) // 2 * 2])
    step = max(1, int(WAVEFORM_SAMPLE_RATE * job.waveform_resolution))
    peaks = [max(map(abs, samples[i:i + step]), default=0) for i in range(0, len(samples), step)]
    top = max(peaks, default=0) or 1
    return [min(255, round(255 * p / top)) for p in peaks]


def validate_tracks(tracks: list, duration: float) -> list[dict]:
    """Normalise and validate a tracks list posted from the editor."""
    if not isinstance(tracks, list) or not tracks:
        raise SplitError("At least one track is required.")
    clean = []
    for i, t in enumerate(tracks, 1):
        try:
            start = float(t["start"])
        except (KeyError, TypeError, ValueError):
            raise SplitError(f"Track {i} has no valid start time.")
        title = str(t.get("title", "")).strip()
        artists = [str(a).strip() for a in t.get("artists", []) if str(a).strip()]
        if not title:
            raise SplitError(f"Track {i} needs a title.")
        if not artists:
            raise SplitError(f"Track {i} ({title}) needs an artist.")
        clean.append({"start": round(start, 2), "title": title, "artists": artists})
    clean.sort(key=lambda t: t["start"])
    clean[0]["start"] = 0.0
    for a, b in zip(clean, clean[1:]):
        if b["start"] - a["start"] < 1:
            raise SplitError(f'"{a["title"]}" is shorter than one second; remove one of its boundaries.')
    if clean[-1]["start"] >= duration - 1:
        raise SplitError("The last track starts at the very end of the recording.")
    return clean


def _safe_filename(s: str) -> str:
    return re.sub(r'[/\\:*?"<>|]', "_", s).strip() or "untitled"


def _remove_previous_import(job: SplitJob) -> None:
    for path in job.output_files:
        Track.objects.filter(file_path=path).delete()
        Path(path).unlink(missing_ok=True)


def cut_and_import(job: SplitJob) -> list[str]:
    """Cut the source per job.tracks, tag each piece, and add it to the library.

    Re-running replaces the files from any previous import of this job.
    """
    tracks = validate_tracks(job.tracks, job.duration)
    src = Path(job.source_path)
    if not src.is_file():
        raise SplitError(f"Source recording not found: {src}")
    dest = Path(settings.MUSIC_LIBRARY_PATH) / job.album_artist / job.dest_folder
    dest.mkdir(parents=True, exist_ok=True)

    _remove_previous_import(job)
    job.output_files = []
    job.save(update_fields=["output_files"])

    total = len(tracks)
    written = []
    for i, t in enumerate(tracks, 1):
        end = tracks[i]["start"] if i < total else job.duration
        out = dest / f"{i:02d} {_safe_filename(t['title'])}{src.suffix.lower()}"
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-ss", f"{t['start']:.2f}", "-to", f"{end:.2f}",
             "-i", str(src), "-map", "0:a", "-c", "copy", "-map_metadata", "-1", str(out)],
            capture_output=True,
        )
        if proc.returncode != 0:
            raise SplitError(f"ffmpeg failed cutting track {i} ({t['title']}): "
                             f"{proc.stderr.decode(errors='replace')[-500:]}")
        audio = mutagen.File(str(out), easy=True)
        if audio is None:
            raise SplitError(f"Could not open {out.name} to write tags.")
        if audio.tags is None:
            audio.add_tags()
        audio.tags["title"] = [t["title"]]
        audio.tags["artist"] = [", ".join(t["artists"])]
        audio.tags["album"] = [job.album_title]
        audio.tags["albumartist"] = [job.album_artist]
        audio.tags["tracknumber"] = [f"{i}/{total}"]
        audio.tags["discnumber"] = ["1/1"]
        if job.year:
            audio.tags["date"] = [str(job.year)]
        if job.genre:
            audio.tags["genre"] = [job.genre]
        audio.save()
        written.append(str(out))
        job.output_files = written
        job.save(update_fields=["output_files"])

    album = None
    with transaction.atomic():
        for path in written:
            tag_data = read_tags(path)
            if tag_data is None:
                raise SplitError(f"Could not read back tags from {Path(path).name}.")
            _upsert_track(tag_data, job.album_artist, source="split recording")
        album = Track.objects.get(file_path=written[0]).album
    if album is not None:
        check_cover_status(album)

    job.status = SplitJob.STATUS_DONE
    job.imported_at = timezone.now()
    job.save(update_fields=["status", "imported_at"])
    return written
