from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("library", "0018_ytdldownload_track_overrides_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="Channel",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=200, unique=True)),
                ("year_min", models.IntegerField(
                    blank=True, null=True,
                    help_text="Oldest release year to include (inclusive). Leave blank for no lower bound.",
                )),
                ("year_max", models.IntegerField(
                    blank=True, null=True,
                    help_text="Newest release year to include (inclusive). Leave blank for no upper bound.",
                )),
                ("genre_group", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="channels",
                    to="library.genregroup",
                    help_text="Only include tracks from this genre group. Leave blank for all genres.",
                )),
                ("artist", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="channels",
                    to="library.artist",
                    help_text="Only include tracks by this artist. Leave blank for all artists.",
                )),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.AddField(
            model_name="playlistitem",
            name="channel",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="playlist_items",
                to="library.channel",
            ),
        ),
    ]
