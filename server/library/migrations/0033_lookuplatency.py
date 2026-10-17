from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0032_splitjob_replaces_albums'),
    ]

    operations = [
        migrations.CreateModel(
            name='LookupLatency',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('service', models.CharField(db_index=True, max_length=50)),
                ('kind', models.CharField(max_length=20)),
                ('title', models.CharField(max_length=500)),
                ('artist', models.CharField(max_length=500)),
                ('seconds', models.FloatField()),
                ('requests', models.PositiveIntegerField(blank=True, null=True)),
                ('ok', models.BooleanField()),
                ('result', models.CharField(blank=True, default='', max_length=200)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
            ],
            options={
                'ordering': ['-created_at'],
                'verbose_name_plural': 'lookup latencies',
            },
        ),
    ]
