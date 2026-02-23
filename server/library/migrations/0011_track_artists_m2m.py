from __future__ import annotations

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0010_remove_track_source_url_track_source"),
    ]

    operations = [
        # Create TrackArtist through model
        migrations.CreateModel(
            name="TrackArtist",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("position", models.PositiveSmallIntegerField()),
                ("artist", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="library.artist")),
                ("track", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="library.track")),
            ],
            options={
                "ordering": ["position"],
                "unique_together": {("track", "position")},
            },
        ),
        # Remove old single-artist FK
        migrations.RemoveField(
            model_name="track",
            name="artist",
        ),
        # Remove album_artist FK (redundant with album.artist)
        migrations.RemoveField(
            model_name="track",
            name="album_artist",
        ),
        # Add the M2M field through TrackArtist
        migrations.AddField(
            model_name="track",
            name="artists",
            field=models.ManyToManyField(related_name="tracks", through="library.TrackArtist", to="library.Artist"),
        ),
    ]
