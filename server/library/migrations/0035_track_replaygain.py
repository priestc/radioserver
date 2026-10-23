from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0034_splitjob_hints"),
    ]

    operations = [
        migrations.AddField(
            model_name="track",
            name="replaygain_track_gain",
            field=models.FloatField(
                blank=True, null=True,
                help_text="ReplayGain track gain in dB, copied from the file's tags.",
            ),
        ),
        migrations.AddField(
            model_name="track",
            name="replaygain_read",
            field=models.BooleanField(
                default=False,
                help_text="Whether replaygain_track_gain has been read from the file yet.",
            ),
        ),
    ]
