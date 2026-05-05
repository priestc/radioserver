from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("library", "0021_videochannel"),
    ]

    operations = [
        migrations.AlterField(
            model_name="videochannel",
            name="video_file_path",
            field=models.CharField(
                blank=True,
                default="",
                max_length=1000,
                help_text="Full path to a local video file, or a YouTube URL. Save to extract 1-fps frames via ffmpeg.",
            ),
        ),
    ]
