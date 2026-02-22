from __future__ import annotations

import random
from collections import deque

from django.core.management.base import BaseCommand

from library.models import GenreGroup, PlaylistItem, PlaylistSettings, Track


class Command(BaseCommand):
    help = "Generate playlist items to fill the given number of hours."

    def add_arguments(self, parser):
        parser.add_argument(
            "hours",
            type=float,
            help="Target duration in hours.",
        )

    def handle(self, *args, **options):
        target_seconds = options["hours"] * 3600

        settings, _ = PlaylistSettings.objects.get_or_create(pk=1)

        tracks = list(
            Track.objects.filter(exclude_from_playlist=False)
            .exclude(duration__isnull=True)
            .select_related("artist")
        )
        if not tracks:
            self.stderr.write("No eligible tracks found.")
            return

        # Build genre -> genre-group lookup
        genre_to_group: dict[str, str] = {}
        for gg in GenreGroup.objects.all():
            for genre in gg.genre_list():
                genre_to_group[genre] = gg.name

        def get_decade(track):
            return (track.year // 10 * 10) if track.year else None

        def get_genre_group(track):
            return genre_to_group.get(track.genre)

        recent_artists: deque[int] = deque(maxlen=settings.artist_skip)
        recent_genres: deque[str | None] = deque(maxlen=settings.genre_skip)
        recent_decades: deque[int | None] = deque(maxlen=settings.decade_skip)

        total_duration = 0.0
        items_created = 0

        while total_duration < target_seconds:
            # Try progressively relaxed filtering
            candidates = None
            for relaxation in range(4):
                candidates = [t for t in tracks if self._passes(
                    t, recent_artists, recent_genres, recent_decades,
                    get_genre_group, get_decade, relaxation,
                )]
                if candidates:
                    break

            if not candidates:
                self.stderr.write("Could not find any valid candidate. Stopping.")
                break

            pick = random.choice(candidates)
            PlaylistItem.objects.create(track=pick)
            items_created += 1
            total_duration += pick.duration

            recent_artists.append(pick.artist_id)
            recent_genres.append(get_genre_group(pick))
            recent_decades.append(get_decade(pick))

        hours = total_duration / 3600
        self.stdout.write(
            f"Created {items_created} playlist items ({hours:.1f} hours)."
        )

    @staticmethod
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
        if relaxation < 3 and track.artist_id in recent_artists:
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
