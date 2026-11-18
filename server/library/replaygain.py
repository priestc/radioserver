"""Measure and tag ReplayGain for tracks as they're queued.

A loudness scan runs ffmpeg over the whole file (several seconds per track), far too
slow to do inside the client sync request that queues the songs, so scans run on one
background worker thread per process. The sync that queued a song may return it
without a gain; later syncs return the measured one and the client picks it up.
"""
from __future__ import annotations

import logging
import queue
import threading

from django.db import close_old_connections, transaction

logger = logging.getLogger(__name__)

_queue: queue.Queue[int] = queue.Queue()
_pending: set[int] = set()
_lock = threading.Lock()
_worker: threading.Thread | None = None


def ensure_replaygain(track) -> None:
    """If *track* has no ReplayGain, schedule a scan for it once the current
    transaction commits (immediately when not in one)."""
    if track.get_replaygain() is not None:
        return
    track_id = track.pk
    transaction.on_commit(lambda: _enqueue(track_id))


def _enqueue(track_id: int) -> None:
    global _worker
    with _lock:
        if track_id in _pending:
            return
        _pending.add(track_id)
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="replaygain-scan", daemon=True)
            _worker.start()
    _queue.put(track_id)


def _run() -> None:
    while True:
        track_id = _queue.get()
        try:
            close_old_connections()
            scan_track(track_id)
        except Exception:
            logger.exception("ReplayGain scan crashed for track %s", track_id)
        finally:
            with _lock:
                _pending.discard(track_id)
            close_old_connections()


def scan_track(track_id: int) -> float | None:
    """Analyze one track's loudness, write ReplayGain tags to its file and store the
    gain. Returns the gain in dB, or None if the scan failed (logged)."""
    from library.management.commands.replaygain import (
        _analyze_loudness, _compute_gain, _write_replaygain_tags,
    )
    from library.models import Track

    try:
        track = Track.objects.get(pk=track_id)
    except Track.DoesNotExist:
        return None

    # Something else (another worker process, an import, the replaygain command) may
    # have tagged it since it was queued.
    track.replaygain_read = False
    if track.get_replaygain() is not None:
        return track.replaygain_track_gain

    loudness = _analyze_loudness(track.file_path)
    if loudness is None:
        logger.warning("ReplayGain: could not analyze loudness of %s", track.file_path)
        return None

    gain_db = _compute_gain(loudness["input_i"])
    if not _write_replaygain_tags(track.file_path, gain_db, loudness["input_tp"]):
        logger.warning("ReplayGain: failed to write tags to %s", track.file_path)
        return None

    track.store_replaygain(gain_db)
    logger.info("ReplayGain: tagged %s at %+.2f dB", track.file_path, gain_db)
    return track.replaygain_track_gain
