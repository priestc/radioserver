from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("library", "0024_remove_videochannel_fps.py"),
    ]

    operations = [
        migrations.AddField(
            model_name="videochannel",
            name="native_fps",
            field=models.FloatField(default=30.0),
        ),
    ]
