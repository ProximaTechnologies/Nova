from django.core.management.base import BaseCommand, CommandError

from config.oidc_bootstrap import bootstrap_grafana_oidc_client


class Command(BaseCommand):
    help = "Create or update the Grafana OIDC client and ensure an RSA signing key exists."

    def add_arguments(self, parser):
        parser.add_argument(
            "--client-name",
            help="OIDC client name to create or update. Defaults to GRAFANA_OAUTH_CLIENT_NAME.",
        )
        parser.add_argument(
            "--client-id",
            help="Client ID to use for a new or existing client. Defaults to the existing DB value or a generated value.",
        )
        parser.add_argument(
            "--client-secret",
            help="Client secret to use for a new or existing client. Defaults to the existing DB value or a generated value.",
        )
        parser.add_argument(
            "--redirect-uri",
            help="Grafana OAuth callback URI. Defaults to <GRAFANA_EXTERNAL_URL>/login/generic_oauth.",
        )
        parser.add_argument(
            "--rotate-client-secret",
            action="store_true",
            help="Generate a new client secret for an existing Grafana OIDC client.",
        )
        parser.add_argument(
            "--sync-grafana",
            action="store_true",
            help="Push the generated OIDC client credentials and org mappings to Grafana.",
        )

    def handle(self, *args, **options):
        try:
            result = bootstrap_grafana_oidc_client(
                client_name=options["client_name"],
                client_id=options["client_id"],
                client_secret=options["client_secret"],
                redirect_uri=options["redirect_uri"],
                rotate_client_secret=options["rotate_client_secret"],
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        if options["sync_grafana"]:
            from config.integration import update_grafana_org_mapping

            try:
                update_grafana_org_mapping()
            except Exception as exc:
                raise CommandError(str(exc)) from exc

        client_action = "created" if result.client_created else "updated"
        key_action = "created" if result.rsa_key_created else "existing"
        sync_message = " Grafana SSO settings synced." if options["sync_grafana"] else ""

        self.stdout.write(
            self.style.SUCCESS(
                "Grafana OIDC bootstrap completed: "
                f"client={client_action} "
                f"client_id={result.client.client_id} "
                f"redirect_uri={result.redirect_uri} "
                f"rsa_key={key_action} "
                f"kid={result.rsa_key.kid}."
                f"{sync_message}"
            )
        )
