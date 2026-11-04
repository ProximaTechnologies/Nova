from django.core.management.base import BaseCommand

from config.integration import update_grafana_org_mapping


class Command(BaseCommand):
    help = "Push the current Nova OIDC and Grafana org mapping settings to Grafana."

    def handle(self, *args, **options):
        update_grafana_org_mapping()
        self.stdout.write(self.style.SUCCESS("Grafana SSO settings synced."))
