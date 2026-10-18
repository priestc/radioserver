from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0033_lookuplatency'),
    ]

    operations = [
        migrations.AddField(
            model_name='splitjob',
            name='hints',
            field=models.JSONField(blank=True, default=dict, help_text='Editor helpers: {"sentences": [[start, end, text]], "suggestions": [{"title", "start", "why"}], "crinkles": [[time, length, strength_db]]}'),
        ),
    ]
