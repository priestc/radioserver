from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("library", "0023_videochannel_fps"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="videochannel",
            name="frames_per_second",
        ),
    ]
