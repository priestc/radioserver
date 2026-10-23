from __future__ import annotations

import secrets

from django.db import models


# Album artist of compilations; each track keeps its own artists.
VARIOUS_ARTISTS = "Various Artists"


class Artist(models.Model):
    name = models.CharField(max_length=500)
    sort_name = models.CharField(max_length=500, blank=True, default="")
    exclude_from_playlist = models.BooleanField(default=False)

    class Meta:
        ordering = ["sort_name", "name"]
        indexes = [models.Index(fields=["name"], name="library_artist_name_idx")]

    def __str__(self):
        return self.name


class Album(models.Model):
    COVER_NONE = ""
    COVER_VALID = "valid"
    COVER_INVALID = "invalid"
    COVER_STATUS_CHOICES = [
        (COVER_NONE, "None"),
        (COVER_VALID, "Valid"),
        (COVER_INVALID, "Invalid"),
    ]

    title = models.CharField(max_length=500)
    artist = models.ForeignKey(
        Artist, on_delete=models.CASCADE, related_name="albums"
    )
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    total_tracks = models.PositiveSmallIntegerField(null=True, blank=True)
    total_discs = models.PositiveSmallIntegerField(null=True, blank=True)
    exclude_from_playlist = models.BooleanField(default=False)
    cover_status = models.CharField(
        max_length=10, choices=COVER_STATUS_CHOICES, default="", blank=True,
    )

    class Meta:
        unique_together = [("title", "artist")]
        ordering = ["artist", "year", "title"]

    def __str__(self):
        return f"{self.artist} — {self.title}"


class Track(models.Model):
    title = models.CharField(max_length=500)
    artists = models.ManyToManyField(
        Artist,
        through="TrackArtist",
        related_name="tracks",
    )
    album = models.ForeignKey(
        Album,
        on_delete=models.CASCADE,
        related_name="tracks",
        null=True,
        blank=True,
    )
    track_number = models.PositiveSmallIntegerField(null=True, blank=True)
    disc_number = models.PositiveSmallIntegerField(null=True, blank=True)
    genre = models.CharField(max_length=200, blank=True, default="")
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    duration = models.FloatField(null=True, blank=True, help_text="Duration in seconds")
    bitrate = models.PositiveIntegerField(null=True, blank=True, help_text="Bitrate in bps")
    sample_rate = models.PositiveIntegerField(null=True, blank=True, help_text="Sample rate in Hz")
    channels = models.PositiveSmallIntegerField(null=True, blank=True)
    exclude_from_playlist = models.BooleanField(default=False)
    file_path = models.CharField(max_length=1000, unique=True)
    file_size = models.PositiveBigIntegerField(null=True, blank=True)
    file_mtime = models.FloatField(null=True, blank=True)
    replaygain_track_gain = models.FloatField(
        null=True, blank=True,
        help_text="ReplayGain track gain in dB, copied from the file's tags.",
    )
    replaygain_read = models.BooleanField(
        default=False,
        help_text="Whether replaygain_track_gain has been read from the file yet.",
    )
    source = models.CharField(max_length=500, blank=True, default="")
    format = models.CharField(max_length=20, blank=True, default="")
    video_path = models.CharField(
        max_length=1000, blank=True, default="",
        help_text="Path to the music video this track's audio was extracted from, if any.",
    )
    date_added = models.DateTimeField(auto_now_add=True)
    date_modified = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["album", "disc_number", "track_number", "title"]
        indexes = [models.Index(fields=["title"], name="library_track_title_idx")]

    def get_replaygain(self) -> float | None:
        """ReplayGain track gain in dB. Read from the file's tags the first time it's
        needed and stored, so syncs don't have to open every queued file again."""
        if not self.replaygain_read:
            from library.tags import read_replaygain
            self.replaygain_track_gain = read_replaygain(self.file_path)
            self.replaygain_read = True
            Track.objects.filter(pk=self.pk).update(
                replaygain_track_gain=self.replaygain_track_gain, replaygain_read=True,
            )
        return self.replaygain_track_gain

    def store_replaygain(self, gain_db: float) -> None:
        """Record a gain that was just written to the file's tags (rounded the same way
        the tag is written, so it matches what a later read would return)."""
        self.replaygain_track_gain = round(gain_db, 2)
        self.replaygain_read = True
        Track.objects.filter(pk=self.pk).update(
            replaygain_track_gain=self.replaygain_track_gain, replaygain_read=True,
        )

    @property
    def display_artist(self) -> str:
        names = list(self.artists.order_by("trackartist__position").values_list("name", flat=True))
        if names:
            return ", ".join(names)
        # A compilation's album artist says nothing about who plays this track.
        if self.album and self.album.artist and self.album.artist.name != VARIOUS_ARTISTS:
            return self.album.artist.name
        return "Unknown Artist"

    def __str__(self):
        album_title = self.album.title if self.album else ""
        parts = [self.display_artist, str(self.year or ""), album_title, self.title]
        return " - ".join(p for p in parts if p)


class TrackArtist(models.Model):
    track = models.ForeignKey(Track, on_delete=models.CASCADE)
    artist = models.ForeignKey(Artist, on_delete=models.CASCADE)
    position = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ["position"]
        unique_together = [("track", "position")]

    def __str__(self):
        return f"{self.track.title} — {self.artist.name} (#{self.position})"


class GenreGroup(models.Model):
    name = models.CharField(max_length=200, unique=True)
    genres = models.TextField(
        help_text="Comma-separated list of genres belonging to this group.",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def genre_list(self):
        return [g.strip() for g in self.genres.split(",") if g.strip()]


class PlaylistSettings(models.Model):
    artist_skip = models.PositiveSmallIntegerField(
        default=3,
        help_text=(
            "Minimum number of songs by other artists that must play "
            "before the same artist can appear again."
        ),
    )
    genre_skip = models.PositiveSmallIntegerField(
        default=3,
        help_text=(
            "Minimum number of songs from other genre groups that must play "
            "before the same genre group can appear again."
        ),
    )
    decade_skip = models.PositiveSmallIntegerField(
        default=3,
        help_text=(
            "Minimum number of songs from other decades that must play "
            "before the same decade can appear again."
        ),
    )

    class Meta:
        verbose_name = "playlist settings"
        verbose_name_plural = "playlist settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    def __str__(self):
        return "Playlist Settings"


class Channel(models.Model):
    name = models.CharField(max_length=200, unique=True)
    year_min = models.IntegerField(
        null=True, blank=True,
        help_text="Oldest release year to include (inclusive). Leave blank for no lower bound.",
    )
    year_max = models.IntegerField(
        null=True, blank=True,
        help_text="Newest release year to include (inclusive). Leave blank for no upper bound.",
    )
    genre_group = models.ForeignKey(
        GenreGroup, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="channels",
        help_text="Only include tracks from this genre group. Leave blank for all genres.",
    )
    genre = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Only include tracks with this exact genre tag. Leave blank for all genres.",
    )
    artist = models.ForeignKey(
        Artist, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="channels",
        help_text="Only include tracks by this artist. Leave blank for all artists.",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Decade(models.Model):
    name = models.CharField(
        max_length=5, unique=True,
        help_text="Must be 4 digits followed by 's', e.g. '1950s'.",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def clean(self):
        import re
        from django.core.exceptions import ValidationError
        if not re.match(r"^\d{4}s$", self.name or ""):
            raise ValidationError(
                {"name": "Decade name must be 4 digits followed by lowercase 's' (e.g. '1950s')."}
            )

    @property
    def slug(self):
        return self.name

    @property
    def year_min(self):
        return int(self.name[:4])

    @property
    def year_max(self):
        return int(self.name[:4]) + 9


class DecadeStation(models.Model):
    decade = models.ForeignKey(Decade, on_delete=models.CASCADE, related_name="stations")
    name = models.CharField(max_length=100)
    slug = models.SlugField()
    genres = models.TextField(
        blank=True, default="",
        help_text="Comma-separated genre tags (exact match). Leave blank to include all genres.",
    )
    genre_group = models.ForeignKey(
        GenreGroup, null=True, blank=True, on_delete=models.SET_NULL,
        help_text="Genre group to match. Takes precedence over the genres field if set.",
    )
    artist = models.ForeignKey(
        Artist, null=True, blank=True, on_delete=models.SET_NULL,
        help_text="Restrict to a specific artist (e.g. for an 'Elvis' station).",
    )

    class Meta:
        unique_together = [("decade", "slug")]
        ordering = ["name"]

    def __str__(self):
        return f"{self.decade.name} — {self.name}"

    def genre_list(self):
        if self.genre_group:
            return self.genre_group.genre_list()
        if self.genres:
            return [g.strip() for g in self.genres.split(",") if g.strip()]
        return []


class PlaylistItem(models.Model):
    track = models.ForeignKey(Track, on_delete=models.CASCADE, related_name="playlist_items")
    channel = models.ForeignKey(
        Channel, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="playlist_items",
    )
    station = models.ForeignKey(
        DecadeStation, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="playlist_items",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    played_at = models.DateTimeField(null=True, blank=True)
    skipped = models.BooleanField(default=False)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"#{self.id} — {self.track}"


def _generate_api_key():
    return secrets.token_hex(32)


class AIServiceManager(models.Model):
    name = models.CharField(max_length=50, unique=True)
    display_name = models.CharField(max_length=100)
    enabled = models.BooleanField(default=True)

    class Meta:
        ordering = ["display_name"]
        verbose_name = "AI service"
        verbose_name_plural = "AI services"

    def __str__(self):
        return self.display_name


class AIServiceError(models.Model):
    service = models.ForeignKey(
        AIServiceManager, on_delete=models.CASCADE, related_name="errors"
    )
    error_message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.service} — {self.created_at:%Y-%m-%d %H:%M}"


class LookupLatency(models.Model):
    """How long one service took to answer one release-year lookup (date finder speed tracking)."""

    service = models.CharField(max_length=50, db_index=True)   # "MusicBrainz", "Discogs", or an AI backend name
    kind = models.CharField(max_length=20)                     # "database", "propose" or "verify"
    title = models.CharField(max_length=500)
    artist = models.CharField(max_length=500)
    seconds = models.FloatField()
    requests = models.PositiveIntegerField(null=True, blank=True)   # HTTP requests made (databases only)
    ok = models.BooleanField()
    result = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "lookup latencies"

    def __str__(self):
        return f"{self.service} {self.seconds:.1f}s — {self.title}"


class YtdlDownload(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("fetching", "Fetching"),
        ("downloading", "Downloading"),
        ("scanning", "Scanning"),
        ("applying_replaygain", "Applying ReplayGain"),
        ("complete", "Complete"),
        ("error", "Error"),
    ]

    url = models.URLField(max_length=500)
    artist_name = models.CharField(max_length=500)
    album_title = models.CharField(max_length=500)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    progress_message = models.TextField(blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    track_overrides = models.JSONField(default=list, blank=True)
    use_track_albums = models.BooleanField(default=False)
    album = models.ForeignKey(
        Album, on_delete=models.SET_NULL, null=True, blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "YouTube download"
        verbose_name_plural = "YouTube downloads"

    def __str__(self):
        return f"{self.artist_name} — {self.album_title}"


class MusicVideoDownload(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("downloading", "Downloading"),
        ("extracting", "Extracting audio"),
        ("scanning", "Scanning"),
        ("applying_replaygain", "Applying ReplayGain"),
        ("complete", "Complete"),
        ("error", "Error"),
    ]

    url = models.URLField(max_length=500)
    title = models.CharField(max_length=500)
    artist_name = models.CharField(max_length=500)
    album_title = models.CharField(max_length=500, blank=True, default="")
    genre = models.CharField(max_length=200, blank=True, default="")
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    thumbnail = models.URLField(max_length=1000, blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    progress_message = models.TextField(blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    video_path = models.CharField(max_length=1000, blank=True, default="")
    track = models.ForeignKey(
        Track, on_delete=models.SET_NULL, null=True, blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "music video download"
        verbose_name_plural = "music video downloads"

    def __str__(self):
        return f"{self.artist_name} — {self.title}"


class ApiKey(models.Model):
    key = models.CharField(max_length=64, unique=True, default=_generate_api_key)
    label = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.label or self.key[:12] + "…"


class VideoChannel(models.Model):
    name = models.CharField(max_length=200, unique=True)
    video_file_path = models.CharField(
        max_length=1000, blank=True, default="",
        help_text="Full path to a local video file, or a YouTube URL. Save to extract frames via ffmpeg.",
    )
    native_fps = models.FloatField(default=30.0)
    frame_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def get_frame_dir(self):
        from django.conf import settings
        from pathlib import Path
        return Path(settings.BASE_DIR) / "video_frames" / str(self.pk)


class SplitJob(models.Model):
    """A long recording (e.g. a radio show episode) to be cut into tracks.

    ``tracks`` is an ordered list of contiguous tracks covering the whole
    recording: [{"start": seconds, "title": str, "artists": [str]}]. Each
    track ends where the next one starts; the last ends at ``duration``.
    """

    STATUS_PENDING = "pending"
    STATUS_DONE = "done"
    STATUS_CHOICES = [(STATUS_PENDING, "Pending"), (STATUS_DONE, "Imported")]

    source_path = models.CharField(max_length=1000, unique=True)
    duration = models.FloatField()
    album_title = models.CharField(max_length=500)
    album_artist = models.CharField(max_length=500)
    dest_folder = models.CharField(
        max_length=500, help_text="Folder name created under <library>/<album artist>/",
    )
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    genre = models.CharField(max_length=200, blank=True, default="")
    playlist = models.JSONField(
        default=list, blank=True,
        help_text='Known songs in order: [{"title", "artists", "expected_length"}]',
    )
    tracks = models.JSONField(default=list, blank=True)
    segments = models.JSONField(
        default=list, blank=True, help_text='Speech/music hints: [["music"|"speech", start, end]]',
    )
    hints = models.JSONField(
        default=dict, blank=True,
        help_text='Editor helpers: {"sentences": [[start, end, text]], "suggestions": [{"title", "start", "why"}], '
                  '"crinkles": [[time, length, strength_db]]}',
    )
    waveform = models.JSONField(default=list, blank=True, help_text="Cached peak values, 0-255")
    waveform_resolution = models.FloatField(default=0.25, help_text="Seconds per waveform value")
    review_notes = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    output_files = models.JSONField(default=list, blank=True)
    replaces_albums = models.JSONField(
        default=list, blank=True,
        help_text="IDs of albums this recording replaces. They're deleted, with their files, "
                  "after this job imports successfully.",
    )
    imported_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["album_title"]

    def __str__(self):
        return self.album_title
