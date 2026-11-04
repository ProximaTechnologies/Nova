import config.models
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("config", "0003_tenantauditlogentry"),
    ]

    operations = [
        migrations.AlterField(
            model_name="supervisorversion",
            name="version",
            field=models.CharField(
                max_length=50,
                unique=True,
                validators=[config.models.validate_supervisor_semver],
            ),
        ),
    ]
