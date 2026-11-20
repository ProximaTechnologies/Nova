"""Repository bootstrap integration tests using local bare Git remotes only."""
import argparse
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import bootstrap_repository as bootstrap
from validate_gitops import documents


class RepositoryBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.remote = self.base / 'remote.git'
        self.checkout = self.base / 'target'
        subprocess.run(['git', 'init', '--bare', '--initial-branch=main', str(self.remote)],
                       check=True, capture_output=True)
        self.env = patch.dict(os.environ, {
            'GIT_AUTHOR_NAME': 'Bootstrap Test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
            'GIT_COMMITTER_NAME': 'Bootstrap Test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.args = argparse.Namespace(repo=str(self.remote), branch='environments/dev',
                                       directory=self.checkout, replace=False)

    def prepare(self):
        with redirect_stdout(io.StringIO()):
            bootstrap.prepare(self.args)
        return self.checkout / 'gitops'

    def publish(self):
        with redirect_stdout(io.StringIO()):
            bootstrap.publish(self.args)

    def test_prepare_publish_custom_branch_and_repeat(self):
        bundle = self.prepare()
        source = bootstrap.root_source(bundle)
        self.assertEqual(source['repoURL'], str(self.remote))
        self.assertEqual(source['targetRevision'], self.args.branch)
        self.assertEqual(source['path'], 'gitops/appsets')
        nova = documents(bundle / 'appsets/Nova.yaml')[0]['spec']['template']['spec']['sources']
        original = documents(bootstrap.SOURCE / 'appsets/Nova.yaml')[0]['spec']['template']['spec']['sources']
        self.assertEqual(nova[0], original[0])  # external chart unchanged
        for item in nova[1:]:
            self.assertEqual(item['repoURL'], str(self.remote))
            self.assertEqual(item['targetRevision'], self.args.branch)
        self.assertEqual((bundle / 'appsets/forgejo-nova.yaml').read_bytes(),
                         (bootstrap.SOURCE / 'appsets/forgejo-nova.yaml').read_bytes())
        self.publish()
        self.assertFalse(bootstrap.git(self.checkout, 'status', '--porcelain'))
        head = bootstrap.git(self.checkout, 'rev-parse', 'HEAD')
        self.assertEqual(bootstrap.git(self.remote, 'rev-parse', f'refs/heads/{self.args.branch}'), head)
        self.publish()
        self.assertEqual(bootstrap.git(self.checkout, 'rev-parse', 'HEAD'), head)
        with self.assertRaisesRegex(ValueError, 'already contains'):
            self.prepare()
        self.args.replace = True
        self.prepare()
        self.assertFalse(bootstrap.git(self.checkout, 'status', '--porcelain'))

    def test_existing_repository_files_are_preserved(self):
        seed = self.base / 'seed'
        subprocess.run(['git', 'clone', str(self.remote), str(seed)], check=True, capture_output=True)
        (seed / 'README.md').write_text('Existing repository content')
        bootstrap.git(seed, 'add', 'README.md')
        bootstrap.git(seed, 'commit', '-m', 'seed')
        bootstrap.git(seed, 'push', 'origin', 'main')
        self.prepare()
        self.assertEqual((self.checkout / 'README.md').read_text(), 'Existing repository content')
        self.publish()
        self.assertEqual(bootstrap.git(self.checkout, 'show', 'HEAD:README.md'), 'Existing repository content')

    def test_empty_main_branch(self):
        self.args.branch = 'main'
        self.prepare()
        self.publish()
        self.assertEqual(bootstrap.git(self.checkout, 'branch', '--show-current'), 'main')

    def test_refuses_changes_outside_bundle(self):
        self.prepare()
        (self.checkout / 'unrelated.txt').write_text('user work')
        with self.assertRaisesRegex(ValueError, 'outside gitops'):
            self.publish()

    def test_refuses_staged_changes(self):
        self.prepare()
        bootstrap.git(self.checkout, 'add', 'gitops/README.md')
        with self.assertRaisesRegex(ValueError, 'Unstage'):
            self.publish()

    def test_install_requires_published_bundle(self):
        self.prepare()
        self.args.kind = None
        with self.assertRaisesRegex(ValueError, 'Publish'):
            bootstrap.install(self.args)
        self.publish()
        (self.checkout / 'gitops/README.md').write_text('changed')
        bootstrap.git(self.checkout, 'add', 'gitops')
        bootstrap.git(self.checkout, 'commit', '-m', 'unpublished')
        with self.assertRaisesRegex(ValueError, 'differs from the remote'):
            bootstrap.install(self.args)

    def test_install_uses_published_target_bundle(self):
        self.prepare()
        self.publish()
        self.args.kind = 'target'
        real_run = subprocess.run
        installs = []

        def run(command, **kwargs):
            if str(command[0]).endswith('install-argocd-kind.sh'):
                installs.append((command, kwargs['env']))
                return subprocess.CompletedProcess(command, 0)
            return real_run(command, **kwargs)

        with patch.object(bootstrap.subprocess, 'run', side_effect=run):
            bootstrap.install(self.args)
        self.assertEqual(installs[0][0], [str(self.checkout.resolve() / 'gitops/argo-cfg/install-argocd-kind.sh'), 'target'])
        self.assertEqual(installs[0][1]['KUBE_CONTEXT'], 'kind-target')

    def test_default_source_unchanged(self):
        original = bootstrap.root_source(bootstrap.SOURCE).copy()
        self.prepare()
        self.assertEqual(bootstrap.root_source(bootstrap.SOURCE), original)
        self.assertEqual(original['repoURL'], bootstrap.DEFAULT_REPO)

    def test_credentials_in_url_rejected(self):
        self.args.repo = 'https://user:secret@example.invalid/repo.git'
        with self.assertRaisesRegex(ValueError, 'credentials'):
            self.prepare()
        self.assertFalse(self.checkout.exists())

    def test_prepare_preserves_unpublished_commits(self):
        self.prepare()
        self.publish()
        (self.checkout / 'notes.txt').write_text('keep')
        bootstrap.git(self.checkout, 'add', 'notes.txt')
        bootstrap.git(self.checkout, 'commit', '-m', 'local work')
        head = bootstrap.git(self.checkout, 'rev-parse', 'HEAD')
        self.args.replace = True
        with self.assertRaisesRegex(ValueError, 'unpublished local commits'):
            self.prepare()
        self.assertEqual(bootstrap.git(self.checkout, 'rev-parse', 'HEAD'), head)


if __name__ == '__main__':
    unittest.main()
