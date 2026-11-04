from django.core.management.base import BaseCommand, CommandError

from config.gitops.sync import sync_gitops_resources


class Command(BaseCommand):
    help = "Render NovaUI GitOps desired state and write CRDs into Forgejo tenant repos or a Git worktree."

    def add_arguments(self, parser):
        parser.add_argument(
            "--plugin",
            action="append",
            dest="plugins",
            help="Plugin name to sync. Can be passed multiple times. Defaults to all configured plugins.",
        )
        parser.add_argument("--tenant-id", type=int, help="Limit sync to one tenant id.")
        parser.add_argument("--reason", default="manual", help="Reason stored on the sync run.")
        parser.add_argument(
            "--render-only",
            action="store_true",
            help="Only store desired state in the DB; do not write manifests to Git.",
        )

    def handle(self, *args, **options):
        try:
            run = sync_gitops_resources(
                plugin_names=options["plugins"],
                tenant_id=options["tenant_id"],
                reason=options["reason"],
                write=not options["render_only"],
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                "GitOps sync completed: "
                f"run={run.pk} rendered={run.rendered_count} "
                f"synced={run.synced_count} pruned={run.pruned_count}"
            )
        )
