from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("library", "0022_videochannel_helptext"),
    ]

    operations = [
        migrations.AddField(
            model_name="videochannel",
            name="frames_per_second",
            field=models.FloatField(
                default=1.0,
                help_text="How many frames per second to extract and display (e.g. 1, 2, 5).",
            ),
        ),
    ]
