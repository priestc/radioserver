from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0029_track_video_path_musicvideodownload"),
    ]

    operations = [
        migrations.AlterField(
            model_name="decade",
            name="id",
            field=models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
        ),
        migrations.AlterField(
            model_name="decadestation",
            name="id",
            field=models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
        ),
        migrations.AlterField(
            model_name="videochannel",
            name="id",
            field=models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
        ),
        migrations.AlterField(
            model_name="videochannel",
            name="video_file_path",
            field=models.CharField(blank=True, default="", help_text="Full path to a local video file, or a YouTube URL. Save to extract frames via ffmpeg.", max_length=1000),
        ),
    ]
