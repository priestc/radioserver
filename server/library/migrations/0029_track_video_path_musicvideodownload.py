from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0028_artist_name_idx_track_title_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="track",
            name="video_path",
            field=models.CharField(
                blank=True, default="", max_length=1000,
                help_text="Path to the music video this track's audio was extracted from, if any.",
            ),
        ),
        migrations.CreateModel(
            name="MusicVideoDownload",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("url", models.URLField(max_length=500)),
                ("title", models.CharField(max_length=500)),
                ("artist_name", models.CharField(max_length=500)),
                ("album_title", models.CharField(blank=True, default="", max_length=500)),
                ("genre", models.CharField(blank=True, default="", max_length=200)),
                ("year", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("thumbnail", models.URLField(blank=True, default="", max_length=1000)),
                ("status", models.CharField(
                    choices=[
                        ("pending", "Pending"),
                        ("downloading", "Downloading"),
                        ("extracting", "Extracting audio"),
                        ("scanning", "Scanning"),
                        ("applying_replaygain", "Applying ReplayGain"),
                        ("complete", "Complete"),
                        ("error", "Error"),
                    ],
                    default="pending", max_length=20,
                )),
                ("progress_message", models.TextField(blank=True, default="")),
                ("error_message", models.TextField(blank=True, default="")),
                ("video_path", models.CharField(blank=True, default="", max_length=1000)),
                ("track", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    to="library.track",
                )),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "music video download",
                "verbose_name_plural": "music video downloads",
                "ordering": ["-created_at"],
            },
        ),
    ]
