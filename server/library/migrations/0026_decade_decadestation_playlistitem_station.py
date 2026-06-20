from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0025_videochannel_native_fps"),
    ]

    operations = [
        migrations.CreateModel(
            name="Decade",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=50, unique=True)),
                ("slug", models.SlugField(unique=True)),
                ("year_min", models.IntegerField()),
                ("year_max", models.IntegerField()),
            ],
            options={
                "ordering": ["year_min"],
            },
        ),
        migrations.CreateModel(
            name="DecadeStation",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100)),
                ("slug", models.SlugField()),
                ("genres", models.TextField(
                    blank=True, default="",
                    help_text="Comma-separated genre tags (exact match). Leave blank to include all genres.",
                )),
                ("decade", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="stations",
                    to="library.decade",
                )),
                ("genre_group", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    to="library.genregroup",
                    help_text="Genre group to match. Takes precedence over the genres field if set.",
                )),
                ("artist", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    to="library.artist",
                    help_text="Restrict to a specific artist (e.g. for an 'Elvis' station).",
                )),
            ],
            options={
                "ordering": ["name"],
                "unique_together": {("decade", "slug")},
            },
        ),
        migrations.AddField(
            model_name="playlistitem",
            name="station",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="playlist_items",
                to="library.decadestation",
            ),
        ),
    ]
