from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("library", "0019_channel"),
    ]

    operations = [
        migrations.AddField(
            model_name="channel",
            name="genre",
            field=models.CharField(
                blank=True,
                default="",
                max_length=200,
                help_text="Only include tracks with this exact genre tag. Leave blank for all genres.",
            ),
        ),
    ]
