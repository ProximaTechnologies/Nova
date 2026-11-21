"""Exercise bootstrap command routing with fake CLIs; never contact a cluster."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.log = self.directory / 'commands'
        for name in ('kubectl', 'helm', 'kind', 'docker'):
            command = self.directory / name
            command.write_text('''#!/usr/bin/env bash
printf '%s\\n' "$(basename "$0") $*" >> "$COMMAND_LOG"
if [[ "$*" == 'config get-contexts -o name' ]]; then printf 'test-cluster\\nkind-argo\\n'; fi
if [[ "$*" == 'get clusters' ]]; then
  [[ "${FAIL_CLUSTERS:-false}" != true ]] || exit 1
  echo "${FAKE_CLUSTERS-argo}"
fi
if [[ "$(basename "$0") $*" == 'docker info' && "${FAIL_DOCKER:-false}" == true ]]; then exit 1; fi
if [[ "$*" == 'get nodes --name argo' ]]; then echo argo-control-plane; fi
if [[ "$*" == 'exec argo-control-plane uname -m' ]]; then echo aarch64; fi
if [[ "$*" == *'Corefile}'* ]]; then printf '.:53 {\\n    kubernetes cluster.local\\n}\\n'; fi
if [[ "$*" == *'images import'* ]]; then cat >/dev/null; fi
if [[ "$*" == *'apply -f -'* ]]; then cat > "$SECRET_CAPTURE"; fi
''')
            command.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('ARGOCD_', 'KUBE_', 'KIND_', 'GITHUB_TOKEN'))}
        self.env.update(PATH=f'{self.directory}:/usr/bin:/bin',
                        COMMAND_LOG=str(self.log),
                        SECRET_CAPTURE=str(self.directory / 'secret'))

    def run_script(self, script, *args, **env):
        return subprocess.run(['bash', str(ROOT / 'argo-cfg' / script), *args],
                              env={**self.env, **env}, capture_output=True, text=True)

    def test_existing_context_is_explicit_for_all_mutations(self):
        result = self.run_script('install-argocd.sh', KUBE_CONTEXT='test-cluster')
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.log.read_text().splitlines()
        self.assertFalse(any('use-context' in c or c.startswith('kind ') for c in commands))
        for command in commands:
            if command.startswith('kubectl ') and 'config get-contexts' not in command:
                self.assertIn('--context test-cluster', command)
            if command.startswith('helm upgrade'):
                self.assertIn('--kube-context test-cluster', command)
                self.assertIn('--version 9.5.15', command)
        self.assertTrue(any('root-app.yaml' in c for c in commands))

    def test_context_required_and_unknown_context_rejected(self):
        for env in ({}, {'KUBE_CONTEXT': 'absent'}):
            result = self.run_script('install-argocd.sh', **env)
            self.assertEqual(result.returncode, 2)
        self.assertNotIn('helm upgrade', self.log.read_text())

    def test_kind_wrapper_passes_context_and_values(self):
        result = self.run_script('install-argocd-kind.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--kube-context kind-argo', self.log.read_text())
        self.assertIn('values-kind.yaml', self.log.read_text())

    def test_reset_requires_force(self):
        result = self.run_script('reset.sh')
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists())

    def test_create_refuses_existing_cluster(self):
        result = self.run_script('create-kind.sh')
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('delete cluster', self.log.read_text())
        self.assertNotIn('create cluster', self.log.read_text())

    def test_create_stops_when_cluster_listing_fails(self):
        result = self.run_script('create-kind.sh', FAIL_CLUSTERS='true')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('kind create cluster', self.log.read_text())

    def test_create_stops_when_docker_is_unavailable(self):
        result = self.run_script('create-kind.sh', FAIL_DOCKER='true')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('kind create cluster', self.log.read_text())

    def test_local_images_load_before_install(self):
        result = self.run_script('create-kind.sh', FAKE_CLUSTERS='',
                                 NOVA_CFG_IMAGE='cfg:local', RUN_DEPLOYMENT_CHECK='false')
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.log.read_text()
        self.assertIn('docker image save --platform linux/arm64 cfg:local', commands)
        self.assertIn('images import --platform linux/arm64 -', commands)
        self.assertLess(commands.index('images import'), commands.index('helm upgrade'))

    def test_credentials_use_stdin_and_do_not_skip_repo_registration(self):
        result = self.run_script('install-argocd.sh', KUBE_CONTEXT='test-cluster',
                                 ARGOCD_REPO_PASSWORD='test-password')
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.log.read_text()
        self.assertIn('repository.yaml', commands)
        self.assertNotIn('test-password', commands + result.stdout + result.stderr)
        self.assertIn('secret-type: repo-creds', (self.directory / 'secret').read_text())

    def test_unsupported_namespace_fails_before_mutation(self):
        result = self.run_script('install-argocd.sh', KUBE_CONTEXT='test-cluster',
                                 ARGOCD_NAMESPACE='other')
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists())


if __name__ == '__main__':
    unittest.main()
