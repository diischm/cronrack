# The network as a third subject of a StatusEvent (network/models.py). One migration:
# the new column, its index, and the "exactly one subject" check widened to three.
#
# SQLite: the column and the index are plain ALTER TABLE / CREATE INDEX; changing the
# CHECK constraint makes Django copy activity_statusevent into a new table once (SQLite
# can't alter a constraint in place). The hosts, clusters and report tables are untouched.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("activity", "0003_statusevent_cluster"),
        ("clusters", "0001_initial"),
        ("hosts", "0004_host_cluster"),
        ("network", "0001_initial"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="statusevent",
            name="status_event_one_subject",
        ),
        migrations.AddField(
            model_name="statusevent",
            name="network",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="status_events",
                to="network.network",
            ),
        ),
        migrations.AddIndex(
            model_name="statusevent",
            index=models.Index(
                fields=["network", "-created_at"], name="activity_st_network_04d036_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="statusevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("cluster__isnull", True),
                        ("host__isnull", False),
                        ("network__isnull", True),
                    ),
                    models.Q(
                        ("cluster__isnull", False),
                        ("host__isnull", True),
                        ("network__isnull", True),
                    ),
                    models.Q(
                        ("cluster__isnull", True),
                        ("host__isnull", True),
                        ("network__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="status_event_one_subject",
            ),
        ),
    ]
