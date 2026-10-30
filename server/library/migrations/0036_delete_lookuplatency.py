from django.db import migrations


class Migration(migrations.Migration):
    """Service response times are now logged by the service delegator
    (github.com/priestc/service-delegator); the rows were imported there."""

    dependencies = [
        ("library", "0035_track_replaygain"),
    ]

    operations = [
        migrations.DeleteModel(name="LookupLatency"),
    ]
