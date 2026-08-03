from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0027_decade_remove_slug_year_fields"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="artist",
            index=models.Index(fields=["name"], name="library_artist_name_idx"),
        ),
        migrations.AddIndex(
            model_name="track",
            index=models.Index(fields=["title"], name="library_track_title_idx"),
        ),
    ]
