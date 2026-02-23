from __future__ import annotations

import random
from collections import deque

from library.models import GenreGroup, PlaylistItem, PlaylistSettings, Track


def _passes(
    track,
    recent_artists,
    recent_genres,
    recent_decades,
    get_genre_group,
    get_decade,
    relaxation,
):
    """Check skip rules with progressive relaxation.

    relaxation 0: all rules
    relaxation 1: drop decade rule
    relaxation 2: drop decade + genre rules
    relaxation 3: drop all rules
    """
    if relaxation < 3:
        track_artist_ids = track._artist_ids
        if any(track_artist_ids & s for s in recent_artists):
            return False
    if relaxation < 2:
        group = get_genre_group(track)
        if group is not None and group in recent_genres:
            return False
    if relaxation < 1:
        decade = get_decade(track)
        if decade is not None and decade in recent_decades:
            return False
    return True


def generate_playlist(target_seconds: float) -> tuple[int, float]:
    """Generate playlist items to fill *target_seconds* of audio.

    Returns (items_created, total_duration_seconds).
    """
    settings, _ = PlaylistSettings.objects.get_or_create(pk=1)

    tracks = list(
        Track.objects.filter(exclude_from_playlist=False)
        .exclude(duration__isnull=True)
        .prefetch_related("artists")
    )
    if not tracks:
        return 0, 0.0

    # Cache artist IDs on each track to avoid repeated queries
    for t in tracks:
        t._artist_ids = set(a.id for a in t.artists.all())

    # Build genre -> genre-group lookup
    genre_to_group: dict[str, str] = {}
    for gg in GenreGroup.objects.all():
        for genre in gg.genre_list():
            genre_to_group[genre] = gg.name

    def get_decade(track):
        return (track.year // 10 * 10) if track.year else None

    def get_genre_group(track):
        return genre_to_group.get(track.genre)

    recent_artists: deque[set[int]] = deque(maxlen=settings.artist_skip)
    recent_genres: deque[str | None] = deque(maxlen=settings.genre_skip)
    recent_decades: deque[int | None] = deque(maxlen=settings.decade_skip)

    total_duration = 0.0
    items_created = 0

    while total_duration < target_seconds:
        candidates = None
        for relaxation in range(4):
            candidates = [t for t in tracks if _passes(
                t, recent_artists, recent_genres, recent_decades,
                get_genre_group, get_decade, relaxation,
            )]
            if candidates:
                break

        if not candidates:
            break

        pick = random.choice(candidates)
        PlaylistItem.objects.create(track=pick)
        items_created += 1
        total_duration += pick.duration

        recent_artists.append(pick._artist_ids)
        recent_genres.append(get_genre_group(pick))
        recent_decades.append(get_decade(pick))

    return items_created, total_duration
