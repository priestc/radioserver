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
        track = {"start": round(start, 2), "title": title, "artists": artists}
        if t.get("excerpt"):
            track["excerpt"] = True
        clean.append(track)
    clean.sort(key=lambda t: t["start"])
    clean[0]["start"] = 0.0
    for a, b in zip(clean, clean[1:]):
        if b["start"] - a["start"] < 1:
            raise SplitError(f'"{a["title"]}" is shorter than one second; remove one of its boundaries.')
    if clean[-1]["start"] >= duration - 1:
        raise SplitError("The last track starts at the very end of the recording.")
    return clean


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


SONG_MIN_MUSIC = 25.0   # music stretches shorter than this aren't treated as songs
MUSIC_GAP_MERGE = 4.0   # music separated by less than this is one stretch (bridges misread vocals)
# Fitted to hand-placed boundaries: Dylan usually says a little about the song
# that ended, then more about the next one, so the switch tends to come ~10s
# after the music stops. He sometimes talks over a song's last bars, and the
# detector still calls that music, so the view starts well before the music ends.
SUGGEST_AFTER_MUSIC = 10.0   # suggested boundary: this long after the music ends...
SUGGEST_MAX_FRACTION = 1 / 3  # ...but no further than this far into the gap
WINDOW_BEFORE = 12.0          # seconds shown before the music ends
WINDOW_AFTER = 6.0            # seconds shown after the next song starts


def song_regions(segments: list) -> list[tuple[float, float]]:
    """Long stretches of detected music, i.e. the songs."""
    regions: list[list[float]] = []
    for lab, s, e in segments:
        if lab != "music":
            continue
        if regions and s - regions[-1][1] < MUSIC_GAP_MERGE:
            regions[-1][1] = e
        else:
            regions.append([s, e])
    return [(s, e) for s, e in regions if e - s >= SONG_MIN_MUSIC]


def boundary_context(segments: list, b: float, duration: float) -> dict:
    """Describe the gap around boundary time b.

    Returns {"suggested", "window": [start, end]}: the suggestion is a little
    after the song ending before b stops (see SUGGEST_AFTER_MUSIC); with no gap,
    the next song's start. If b sits inside a song (back-to-back songs), it's
    left where it is.
    """
    regions = song_regions(segments)
    before = [r for r in regions if r[0] < b - 1]
    if not before:
        nxt = next((r for r in regions if r[0] >= b - 1), None)
        end = nxt[0] + WINDOW_AFTER if nxt else min(duration, b + 15)
        return {"suggested": b, "window": [0.0, max(end, b + 10)]}
    a = before[-1]
    if b < a[1] - 3:
        return {"suggested": b, "window": [max(0.0, b - 12), min(duration, b + 12)]}
    nxt = next((r for r in regions if r[0] >= a[1]), None)
    gap_end = nxt[0] if nxt else duration
    suggested = a[1] + min(SUGGEST_AFTER_MUSIC, (gap_end - a[1]) * SUGGEST_MAX_FRACTION)
    lo, hi = max(0.0, a[1] - WINDOW_BEFORE), min(duration, gap_end + WINDOW_AFTER)
    if hi - lo < 10:
        lo, hi = max(0.0, suggested - 5), min(duration, suggested + 5)
    return {"suggested": round(suggested, 2), "window": [round(lo, 2), round(hi, 2)]}


def suggest_boundaries(job: SplitJob) -> list[dict]:
    """Return job.tracks with every boundary moved to its suggested point."""
    tracks = [dict(t) for t in job.tracks]
    for i in range(1, len(tracks)):
        tracks[i]["start"] = boundary_context(job.segments, tracks[i]["start"], job.duration)["suggested"]
    # Keep order and at least a second per track if two suggestions collide.
    for i in range(1, len(tracks)):
        tracks[i]["start"] = max(tracks[i]["start"], tracks[i - 1]["start"] + 1)
    return tracks


def expected_track_count(job: SplitJob) -> int | None:
    """Opening + one track per playlist song + Closing, when the job has a playlist."""
    return len(job.playlist) + 2 if job.playlist else None


def canonical_tracks(job: SplitJob) -> tuple[list[dict], list[str]]:
    """Return (tracks, inserted titles) with exactly Opening + playlist songs + Closing.

    Existing tracks are matched to that list in order (an LCS on normalised
    titles, so duplicate titles in a playlist are fine) and keep their start,
    name and excerpt flag. Unmatched talk tracks are dropped, which merges them
    into the track before. Songs with no track yet are inserted at the next
    music breaks between their neighbours, or spread evenly if there are none.
    """
    artist = job.album_artist
    want = ([{"title": "Opening", "artists": [artist]}]
            + [{"title": p["title"], "artists": list(p["artists"])} for p in job.playlist]
            + [{"title": "Closing", "artists": [artist]}])
    have = [t for t in job.tracks if t["start"] >= 1 or _norm(t["title"]) == "opening"]
    a, b = [_norm(w["title"]) for w in want], [_norm(t["title"]) for t in have]
    lcs = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) - 1, -1, -1):
        for j in range(len(b) - 1, -1, -1):
            lcs[i][j] = lcs[i + 1][j + 1] + 1 if a[i] == b[j] else max(lcs[i + 1][j], lcs[i][j + 1])
    match, i, j = {}, 0, 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            match[i] = have[j]; i += 1; j += 1
        elif lcs[i + 1][j] >= lcs[i][j + 1]:
            i += 1
        else:
            j += 1
    match[0] = {**match.get(0, want[0]), "start": 0.0}

    out = []
    for k, w in enumerate(want):
        m = match.get(k)
        t = {"start": m["start"] if m else None, "title": m["title"] if m else w["title"],
             "artists": list(m["artists"]) if m else w["artists"]}
        if m and m.get("excerpt"):
            t["excerpt"] = True
        out.append(t)

    regions = song_regions(job.segments)
    breaks = [r1[1] + min(SUGGEST_AFTER_MUSIC, (r2[0] - r1[1]) * SUGGEST_MAX_FRACTION)
              for r1, r2 in zip(regions, regions[1:])]
    inserted = []
    k = 1
    while k < len(out):
        if out[k]["start"] is not None:
            k += 1
            continue
        run_end = k
        while run_end < len(out) and out[run_end]["start"] is None:
            run_end += 1
        lo = out[k - 1]["start"]
        hi = out[run_end]["start"] if run_end < len(out) else job.duration
        n = run_end - k
        pts = [p for p in breaks if lo + 10 < p < hi - 10][:n]
        while len(pts) < n:
            base = pts[-1] if pts else lo
            pts.append(base + (hi - base) / (n - len(pts) + 1))
        for idx, p in zip(range(k, run_end), pts):
            out[idx]["start"] = round(p, 2)
            inserted.append(out[idx]["title"])
        k = run_end
    for k in range(1, len(out)):
        out[k]["start"] = round(max(out[k]["start"], out[k - 1]["start"] + 1), 2)
    return out, inserted


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
    if not expected:
        return None
    end = tracks[i + 1]["start"] if i + 1 < len(tracks) else job.duration
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
    used = {_norm(t["title"]) for t in job.tracks}
    issues = []
    missing = [p["title"] for p in job.playlist if _norm(p["title"]) not in used]
    if missing:
        issues.append("Not on any track yet: " + ", ".join(missing))
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
