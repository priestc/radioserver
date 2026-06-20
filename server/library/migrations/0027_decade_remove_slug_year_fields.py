from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0026_decade_decadestation_playlistitem_station"),
    ]

    operations = [
        migrations.RemoveField(model_name="decade", name="slug"),
        migrations.RemoveField(model_name="decade", name="year_min"),
        migrations.RemoveField(model_name="decade", name="year_max"),
        migrations.AlterField(
            model_name="decade",
            name="name",
            field=models.CharField(
                max_length=5,
                unique=True,
                help_text="Must be 4 digits followed by 's', e.g. '1950s'.",
            ),
        ),
        migrations.AlterModelOptions(
            name="decade",
            options={"ordering": ["name"]},
        ),
    ]
