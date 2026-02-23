from __future__ import annotations

import secrets

from django.db import models


class Artist(models.Model):
    name = models.CharField(max_length=500)
    sort_name = models.CharField(max_length=500, blank=True, default="")
    exclude_from_playlist = models.BooleanField(default=False)

    class Meta:
        ordering = ["sort_name", "name"]

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
    source = models.CharField(max_length=500, blank=True, default="")
    format = models.CharField(max_length=20, blank=True, default="")
    date_added = models.DateTimeField(auto_now_add=True)
    date_modified = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["album", "disc_number", "track_number", "title"]

    @property
    def display_artist(self) -> str:
        names = list(self.artists.order_by("trackartist__position").values_list("name", flat=True))
        if names:
            return ", ".join(names)
        if self.album and self.album.artist:
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


class PlaylistItem(models.Model):
    track = models.ForeignKey(Track, on_delete=models.CASCADE, related_name="playlist_items")
    started_at = models.DateTimeField(null=True, blank=True)
    played_at = models.DateTimeField(null=True, blank=True)
    skipped = models.BooleanField(default=False)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"#{self.id} — {self.track}"


def _generate_api_key():
    return secrets.token_hex(32)


class ApiKey(models.Model):
    key = models.CharField(max_length=64, unique=True, default=_generate_api_key)
    label = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.label or self.key[:12] + "…"
