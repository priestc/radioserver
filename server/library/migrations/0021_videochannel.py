from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("library", "0020_channel_genre"),
    ]

    operations = [
        migrations.CreateModel(
            name="VideoChannel",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=200, unique=True)),
                ("video_file_path", models.CharField(
                    blank=True,
                    default="",
                    max_length=1000,
                    help_text="Full path to the source video file. Save to extract 1-fps frames via ffmpeg.",
                )),
                ("frame_count", models.IntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["name"],
            },
        ),
    ]
