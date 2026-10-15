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
# Browsers seek VBR MP3s (especially ones without a Xing seek table) by
# estimating a byte offset, so playback can drift seconds away from the
# reported currentTime. The editor plays a constant-bitrate copy instead,
# which seeks exactly; cuts are still made from the original file.
PROXY_DIR = Path.home() / ".radioserver" / "splitter_proxies"


class SplitError(Exception):
    pass


def proxy_path(job: SplitJob) -> Path:
    return PROXY_DIR / f"{job.pk}.mp3"


def ensure_playback_proxy(job: SplitJob) -> Path:
    """Return a CBR MP3 copy of the job's source for accurate in-browser seeking."""
    out = proxy_path(job)
    src = Path(job.source_path)
    if out.is_file() and out.stat().st_mtime >= src.stat().st_mtime:
        return out
    PROXY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.mp3")
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:a", "-c:a", "libmp3lame",
         "-b:a", "128k", "-ar", "44100", "-write_xing", "0", "-map_metadata", "-1", str(tmp)],
        capture_output=True,
    )
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise SplitError(f"ffmpeg could not make a playback copy of {src.name}: "
                         f"{proc.stderr.decode(errors='replace')[-500:]}")
    tmp.replace(out)
    return out


def compute_waveform(job: SplitJob) -> tuple[list[int], float]:
    """Decode the source to mono PCM; return (one peak 0-255 per resolution step, exact duration).

    The decoded duration is authoritative: header-based estimates are wrong for
    VBR MP3s that lack a seek table.
    """
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
    return [min(255, round(255 * p / top)) for p in peaks], len(samples) / WAVEFORM_SAMPLE_RATE


def validate_tracks(tracks: list, duration: float, require_all: bool = False) -> list[dict]:
    """Normalise and validate a tracks list posted from the editor.

    The order is fixed by the playlist. A boundary the user hasn't placed yet has
    start None; that's fine while editing, but cutting needs every one placed
    (require_all).
    """
    if not isinstance(tracks, list) or not tracks:
        raise SplitError("At least one track is required.")
    clean = []
    for i, t in enumerate(tracks, 1):
        try:
            start = None if t["start"] is None and i > 1 else float(t["start"])
        except (KeyError, TypeError, ValueError):
            raise SplitError(f"Track {i} has no valid start time.")
        title = str(t.get("title", "")).strip()
        artists = [str(a).strip() for a in t.get("artists", []) if str(a).strip()]
        if not title:
            raise SplitError(f"Track {i} needs a title.")
        if not artists:
            raise SplitError(f"Track {i} ({title}) needs an artist.")
        track = {"start": None if start is None else round(start, 2), "title": title, "artists": artists}
        if t.get("excerpt"):
            track["excerpt"] = True
        clean.append(track)
    clean[0]["start"] = 0.0
    unplaced = [t["title"] for t in clean if t["start"] is None]
    if require_all and unplaced:
        raise SplitError(f"{len(unplaced)} boundar{'y has' if len(unplaced) == 1 else 'ies have'} not been placed yet "
                         f"(the start of {', '.join(unplaced[:3])}{'…' if len(unplaced) > 3 else ''}).")
    placed = [t for t in clean if t["start"] is not None]
    for a, b in zip(placed, placed[1:]):
        if b["start"] - a["start"] < 1:
            raise SplitError(f'The start of "{b["title"]}" must come after the start of "{a["title"]}".')
    if len(placed) > 1 and placed[-1]["start"] >= duration - 1:
        raise SplitError(f'"{placed[-1]["title"]}" starts at the very end of the recording.')
    return clean


def unplaced_count(job: SplitJob) -> int:
    return sum(1 for t in job.tracks[1:] if t.get("start") is None)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def expected_track_count(job: SplitJob) -> int | None:
    """Opening + one track per playlist song + Closing, when the job has a playlist."""
    return len(job.playlist) + 2 if job.playlist else None


def music_seconds(segments: list, start: float, end: float) -> float:
    """Seconds of detected music between start and end."""
    return sum(max(0.0, min(e, end) - max(s, start)) for lab, s, e in segments if lab == "music")


SHORT_RATIO = 0.6   # less music than this fraction of the usual length is "short"
LONG_RATIO = 1.2    # more than this is "long"
EXCERPT_SUFFIX = " (Excerpt)"
# Show intros/outros are kept in the album but never picked for radio playlists.
NOT_FOR_PLAYLISTS = {"Opening", "Closing"}
TALK_TITLES = {"Opening", "Commentary", "Closing"}


def is_talk(job: SplitJob, track: dict) -> bool:
    """Host talk (intro, outro, unrelated banter) rather than a song."""
    return track["title"] in TALK_TITLES and track["artists"] == [job.album_artist]


def track_year(job: SplitJob, tracks: list[dict], i: int) -> int | None:
    """Year to tag on tracks[i]: the broadcast year for talk, the song's own release year
    (from the playlist) for songs. None when a song's release year isn't known."""
    t = tracks[i]
    if is_talk(job, t):
        return job.year
    playlist = job.playlist
    # Track k is playlist song k when the job has the standard Opening + songs + Closing shape.
    if len(tracks) == len(playlist) + 2 and 1 <= i <= len(playlist):
        return playlist[i - 1].get("year")
    match = next((p for p in playlist if _norm(p["title"]) == _norm(t["title"])), None)
    return match.get("year") if match else None


def length_status(job: SplitJob, i: int) -> tuple[str, float, float] | None:
    """Return ("short" | "long" | "ok", music heard, usual length) for a song track.

    Only the detected music is measured, since tracks also carry Dylan's talk;
    with no speech/music hints, the whole track length is used (plus a talk
    allowance). Returns None for talk tracks and songs with no known length.
    """
    tracks = job.tracks
    t = tracks[i]
    p = next((p for p in job.playlist if _norm(p["title"]) == _norm(t["title"])), None)
    expected = p and p.get("expected_length")
    if not expected or t["start"] is None:
        return None
    end = tracks[i + 1]["start"] if i + 1 < len(tracks) else job.duration
    if end is None:
        return None
    if job.segments:
        heard, hi = music_seconds(job.segments, t["start"], end), expected * LONG_RATIO
    else:
        heard, hi = end - t["start"], expected * LONG_RATIO + 150
    if heard < expected * SHORT_RATIO:
        return "short", heard, expected
    if heard > hi:
        return "long", heard, expected
    return "ok", heard, expected


def length_issues(job: SplitJob) -> list[str]:
    """Explain suspicious song lengths.

    A short song next to a long one means the boundary between them is
    probably misplaced. A short song on its own is most likely an excerpt
    (unless it's already marked as one); a long one on its own probably has
    another song hiding in it.
    """
    tracks = job.tracks
    st = [length_status(job, i) for i in range(len(tracks))]
    kind = [x[0] if x else None for x in st]
    issues, paired = [], set()
    for i in range(len(tracks) - 1):
        if {kind[i], kind[i + 1]} == {"short", "long"}:
            paired |= {i, i + 1}
            issues.append(f"The boundary between {tracks[i]['title']} and {tracks[i + 1]['title']} looks off: "
                          f"one has {st[i][1]:.0f}s of music (usually {st[i][2]:.0f}s), "
                          f"the other {st[i + 1][1]:.0f}s (usually {st[i + 1][2]:.0f}s)")
    for i, x in enumerate(st):
        if not x or i in paired:
            continue
        k, heard, expected = x
        if k == "short" and not tracks[i].get("excerpt"):
            issues.append(f"{tracks[i]['title']} has only {heard:.0f}s of music (usually {expected:.0f}s): "
                          "if that's right, mark it as an excerpt")
        elif k == "long":
            issues.append(f"{tracks[i]['title']} has {heard:.0f}s of music (usually {expected:.0f}s): "
                          "the next boundary may be too late")
    return issues


def job_issues(job: SplitJob) -> list[str]:
    """Problems with the job's current tracks, matching the editor's live checks."""
    issues = []
    left = unplaced_count(job)
    if left:
        issues.append(f"{left} of {len(job.tracks) - 1} boundaries still to place")
    return issues + length_issues(job)


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
    tracks = validate_tracks(job.tracks, job.duration, require_all=True)
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
        title = t["title"]
        if t.get("excerpt") and not title.endswith(EXCERPT_SUFFIX):
            title += EXCERPT_SUFFIX
        out = dest / f"{i:02d} {_safe_filename(title)}{src.suffix.lower()}"
        proc = subprocess.run(
            # -ss/-to after -i: read up to the cut point rather than jumping to an
            # estimated byte offset, which is inaccurate for VBR MP3s.
            ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-ss", f"{t['start']:.2f}", "-to", f"{end:.2f}",
             "-map", "0:a", "-c", "copy", "-map_metadata", "-1", str(out)],
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
        audio.tags["title"] = [title]
        audio.tags["artist"] = [", ".join(t["artists"])]
        audio.tags["album"] = [job.album_title]
        audio.tags["albumartist"] = [job.album_artist]
        audio.tags["tracknumber"] = [f"{i}/{total}"]
        audio.tags["discnumber"] = ["1/1"]
        year = track_year(job, tracks, i - 1)
        if year:
            audio.tags["date"] = [str(year)]
        if job.genre:
            audio.tags["genre"] = [job.genre]
        audio.save()
        written.append(str(out))
        job.output_files = written
        job.save(update_fields=["output_files"])

    album = None
    with transaction.atomic():
        for path, t in zip(written, tracks):
            tag_data = read_tags(path)
            if tag_data is None:
                raise SplitError(f"Could not read back tags from {Path(path).name}.")
            _upsert_track(tag_data, job.album_artist, source="split recording")
            if t["title"] in NOT_FOR_PLAYLISTS:
                Track.objects.filter(file_path=path).update(exclude_from_playlist=True)
        album = Track.objects.get(file_path=written[0]).album
    if album is not None:
        check_cover_status(album)

    job.status = SplitJob.STATUS_DONE
    job.imported_at = timezone.now()
    job.save(update_fields=["status", "imported_at"])
    job.removed_albums = remove_replaced_albums(job, keep=written)
    return written


def remove_replaced_albums(job: SplitJob, keep: list[str]) -> list[str]:
    """Delete the albums this job replaces (DB rows and audio files). Returns their titles.

    Runs only after a successful import. Never touches a file this import wrote: when
    the old album has the same title, the import's tracks join it, so only the old
    tracks are removed and the album itself is kept.
    """
    from library.admin import _delete_file_and_cleanup_dir
    from library.models import Album

    removed = []
    keep_set = set(keep)
    for album_id in job.replaces_albums:
        album = Album.objects.filter(pk=album_id).first()
        if album is None:
            continue
        tracks = [t for t in album.tracks.all() if t.file_path not in keep_set]
        for t in tracks:
            if t.file_path:
                _delete_file_and_cleanup_dir(Path(t.file_path))
        title = album.title
        Track.objects.filter(pk__in=[t.pk for t in tracks]).delete()
        if not album.tracks.exists():
            Album.objects.filter(pk=album_id).delete()
        removed.append(f"{title} ({len(tracks)} old tracks)")
    if removed:
        job.replaces_albums = []
        job.review_notes = (job.review_notes + " " if job.review_notes else "") + "Replaced: " + "; ".join(removed)
        job.save(update_fields=["replaces_albums", "review_notes"])
    return removed
