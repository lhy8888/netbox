import django.db.models.functions.comparison
from django.db import migrations, models

import ipam.fields
import ipam.lookups


class Migration(migrations.Migration):
    dependencies = [
        ('ipam', '0089_default_ordering_indexes'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='iprange',
            index=models.Index(
                django.db.models.functions.comparison.Cast(
                    ipam.lookups.Host('start_address'),
                    output_field=ipam.fields.IPAddressField(),
                ),
                name='ipam_iprange_start_host',
            ),
        ),
        migrations.AddIndex(
            model_name='iprange',
            index=models.Index(
                django.db.models.functions.comparison.Cast(
                    ipam.lookups.Host('end_address'),
                    output_field=ipam.fields.IPAddressField(),
                ),
                name='ipam_iprange_end_host',
            ),
        ),
    ]
