import os
import subprocess
from pathlib import Path

import yaml
from django.conf import settings


class GitOpsRepositoryNotConfigured(Exception):
    pass


class GitOpsWriter:
    def __init__(self, repo_path=None, manifest_root=None):
        repo_path = repo_path or settings.GITOPS_REPO_PATH
        if not repo_path:
            raise GitOpsRepositoryNotConfigured("GITOPS_REPO_PATH is not configured.")
        self.repo_path = Path(repo_path).resolve()
        self.manifest_root = (manifest_root or settings.GITOPS_MANIFEST_ROOT).strip("/")

    @property
    def root_path(self):
        return self.repo_path / self.manifest_root

    def write_resource(self, resource):
        path = self._resource_path(resource.manifest_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._to_yaml(resource.desired_manifest), encoding="utf-8")
        return path

    def delete_resource(self, resource):
        if not resource.manifest_path:
            return False
        path = self._resource_path(resource.manifest_path)
        if not path.exists():
            return False
        path.unlink()
        self._cleanup_empty_dirs(path.parent)
        return True

    def commit(self, message):
        if not settings.GITOPS_AUTO_COMMIT:
            return False

        self._run_git("add", self.manifest_root)
        status = self._run_git("status", "--porcelain", "--", self.manifest_root).stdout.strip()
        if not status:
            return False

        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = settings.GITOPS_COMMIT_AUTHOR_NAME
        env["GIT_AUTHOR_EMAIL"] = settings.GITOPS_COMMIT_AUTHOR_EMAIL
        env["GIT_COMMITTER_NAME"] = settings.GITOPS_COMMIT_AUTHOR_NAME
        env["GIT_COMMITTER_EMAIL"] = settings.GITOPS_COMMIT_AUTHOR_EMAIL
        self._run_git("commit", "-m", message, env=env)
        if settings.GITOPS_AUTO_PUSH:
            self._run_git("push")
        return True

    def _resource_path(self, relative_path):
        path = (self.root_path / relative_path).resolve()
        root = self.root_path.resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"Manifest path escapes GitOps root: {relative_path}")
        return path

    def _cleanup_empty_dirs(self, path):
        root = self.root_path.resolve()
        path = path.resolve()
        while path != root and path.is_relative_to(root):
            try:
                path.rmdir()
            except OSError:
                return
            path = path.parent

    def _run_git(self, *args, env=None):
        return subprocess.run(
            ["git", *args],
            cwd=self.repo_path,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )

    def _to_yaml(self, manifest):
        return yaml.safe_dump(manifest, sort_keys=False, explicit_start=True)
