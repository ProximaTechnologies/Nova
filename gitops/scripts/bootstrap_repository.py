#!/usr/bin/env python3
"""Prepare, publish, and install Nova GitOps using the source repo or a target repo."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

from validate_gitops import documents, validate

SOURCE = Path(__file__).resolve().parents[1]
DEFAULT_REPO = 'https://github.com/ProximaTechnologies/Nova.git'


def git(directory, *args, capture=True):
    result = subprocess.run(['git', '-C', str(directory), *args], check=True,
                            text=True, stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else ''


def check_bundle(bundle):
    errors = validate(bundle)
    if errors:
        raise ValueError('\n'.join(errors))


def root_source(bundle):
    return documents(bundle / 'argo-cfg/root-app.yaml')[0]['spec']['source']


def rewrite(bundle, repo, branch):
    """Change only sources owned by this bundle, preserving Helm values/comments."""
    import re
    old_repo = root_source(bundle)['repoURL']
    # Source fields are rewritten as YAML scalars, rather than replacing arbitrary
    # occurrences in scripts, credentials, Helm values or external chart sources.
    paths = [bundle / 'argo-cfg/root-app.yaml', bundle / 'argo-cfg/repository.yaml',
             *sorted((bundle / 'appsets').glob('*.yaml'))]
    for path in paths:
        lines = path.read_text().splitlines(keepends=True)
        for i, line in enumerate(lines):
            match = re.match(r'^(\s*(?:- )?(?:repoURL|url): )(.+?)\s*$', line)
            if match:
                # Parse scalar quoting without reserializing the entire manifest.
                import yaml
                if yaml.safe_load(match[2]) != old_repo:
                    continue
                lines[i] = match[1] + json.dumps(repo) + '\n'
                if 'repoURL:' in match[1]:
                    # targetRevision belongs to the same source mapping.
                    indent = len(line) - len(line.lstrip()) + (2 if line.lstrip().startswith('- ') else 0)
                    for j in range(i + 1, len(lines)):
                        following = lines[j]
                        if following.strip() and len(following) - len(following.lstrip()) < indent:
                            break
                        if following.lstrip().startswith('targetRevision:') and len(following) - len(following.lstrip()) == indent:
                            lines[j] = ' ' * indent + 'targetRevision: ' + json.dumps(branch) + '\n'
                            break
            elif path.name == 'projects.yaml':
                match = re.match(r'^(\s*- )(.+?)\s*$', line)
                if match:
                    import yaml
                    if yaml.safe_load(match[2]) == old_repo:
                        lines[i] = match[1] + json.dumps(repo) + '\n'
        path.write_text(''.join(lines))


def prepare(args):
    destination = args.directory.resolve()
    if destination.is_relative_to(SOURCE.parent) or SOURCE.is_relative_to(destination):
        raise ValueError('Use a separate target checkout, not the Nova source checkout.')
    parsed = urlsplit(args.repo)
    if parsed.scheme in ('http', 'https') and (parsed.username or parsed.password):
        raise ValueError('Do not embed credentials in --repo; use your Git credential helper.')
    # Validate branch names before any clone/write, including option-like input.
    subprocess.run(['git', 'check-ref-format', '--branch', args.branch], check=True,
                   stdout=subprocess.DEVNULL)
    if args.branch.startswith('-') or args.repo.startswith('-'):
        raise ValueError('Repository and branch must not start with a dash.')
    if destination.exists():
        if not (destination / '.git').exists():
            raise ValueError('Destination exists and is not a Git checkout; choose a new directory.')
        if git(destination, 'remote', 'get-url', 'origin') != args.repo:
            raise ValueError('Destination origin does not match --repo.')
        if git(destination, 'status', '--porcelain'):
            raise ValueError('Target checkout must be clean before preparation.')
        git(destination, 'fetch', 'origin')
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['git', 'clone', '--', args.repo, str(destination)], check=True)
    remote_ref = f'refs/remotes/origin/{args.branch}'
    exists = subprocess.run(['git', '-C', str(destination), 'show-ref', '--verify', '--quiet', remote_ref]).returncode == 0
    if exists:
        local_exists = subprocess.run(['git', '-C', str(destination), 'show-ref', '--verify', '--quiet',
                                       f'refs/heads/{args.branch}']).returncode == 0
        if local_exists:
            if git(destination, 'rev-list', f'{remote_ref}..refs/heads/{args.branch}'):
                raise ValueError('Target branch has unpublished local commits; publish or use a fresh checkout.')
            git(destination, 'checkout', args.branch)
            git(destination, 'merge', '--ff-only', remote_ref)
        else:
            git(destination, 'checkout', '-b', args.branch, remote_ref)
    else:
        # A new target branch starts at the remote default branch (or is unborn
        # for an empty repository). Never replace an existing local branch.
        git(destination, 'checkout', '-b', args.branch) if git(destination, 'branch', '--show-current') != args.branch else None
    bundle = destination / 'gitops'
    if bundle.exists() and not args.replace:
        raise ValueError('Target already contains gitops/. Use --replace to replace that bundle explicitly.')
    if bundle.is_symlink():
        raise ValueError('Target gitops/ must not be a symlink.')
    if bundle.exists():
        shutil.rmtree(bundle)
    shutil.copytree(SOURCE, bundle, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.env', '.git'))
    installation = SOURCE.parent / 'Installation.md'
    if not installation.exists():
        installation = SOURCE / 'INSTALLATION.md'
    (bundle / 'INSTALLATION.md').write_text(installation.read_text().replace('(gitops/README.md)', '(README.md)'))
    rewrite(bundle, args.repo, args.branch)
    # README links in a standalone target must not require the Nova source tree.
    readme = bundle / 'README.md'
    readme.write_text(readme.read_text().replace('../Installation.md', 'INSTALLATION.md'))
    check_bundle(bundle)
    print(f'Prepared {bundle} for {args.repo} at branch {args.branch}.')
    print('Review/customize the generated values, then run publish and install with this --directory.')


def target(directory):
    directory = directory.resolve()
    bundle = directory / 'gitops'
    check_bundle(bundle)
    source = root_source(bundle)
    if git(directory, 'remote', 'get-url', 'origin') != source['repoURL']:
        raise ValueError('origin must match the generated root Application repoURL.')
    if git(directory, 'branch', '--show-current') != source['targetRevision']:
        raise ValueError('Current branch must match the generated targetRevision.')
    return directory, bundle, source


def publish(args):
    directory, bundle, source = target(args.directory)
    # Refuse pre-staged work and changes outside the generated bundle.
    staged = git(directory, 'diff', '--cached', '--name-only')
    if staged:
        raise ValueError('Unstage existing changes before publishing the bundle.')
    outside = git(directory, 'status', '--porcelain', '--', '.', ':!gitops')
    if outside:
        raise ValueError('Target checkout has changes outside gitops/; commit or remove them first.')
    git(directory, 'var', 'GIT_AUTHOR_IDENT')
    git(directory, 'var', 'GIT_COMMITTER_IDENT')
    git(directory, 'add', '--', 'gitops')
    if git(directory, 'diff', '--cached', '--name-only'):
        git(directory, 'commit', '-m', 'Configure Nova GitOps bootstrap', capture=False)
    git(directory, 'push', 'origin', f'HEAD:refs/heads/{source["targetRevision"]}', capture=False)
    print('GitOps bundle published. Argo CD can now read the configured branch.')


def install(args):
    bundle = args.directory.resolve() / 'gitops' if args.directory else SOURCE
    check_bundle(bundle)
    if args.directory:
        directory, bundle, source = target(args.directory)
        if git(directory, 'status', '--porcelain', '--', 'gitops'):
            raise ValueError('Publish the GitOps changes before installing.')
        git(directory, 'fetch', 'origin', source['targetRevision'])
        if git(directory, 'diff', 'FETCH_HEAD', '--', 'gitops'):
            raise ValueError('Local bundle differs from the remote branch; publish before installing.')
    env = os.environ.copy()
    script = 'install-argocd.sh'
    extra = []
    if args.kind:
        env['KUBE_CONTEXT'] = f'kind-{args.kind}'
        script = 'install-argocd-kind.sh'
        extra = [args.kind]
    subprocess.run([str(bundle / 'argo-cfg' / script), *extra], env=env, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('prepare', help='Clone an existing target repo and generate its gitops/ bundle')
    command.add_argument('--repo', default=DEFAULT_REPO)
    command.add_argument('--branch', default='main')
    command.add_argument('--directory', required=True, type=Path)
    command.add_argument('--replace', action='store_true', help='Replace an existing clean target gitops/ bundle')
    command.set_defaults(run=prepare)
    command = commands.add_parser('publish', help='Commit only gitops/ and push without force')
    command.add_argument('--directory', required=True, type=Path)
    command.set_defaults(run=publish)
    command = commands.add_parser('install', help='Install Argo CD; defaults to this Nova checkout configuration')
    command.add_argument('--directory', type=Path, help='Use a prepared and published target checkout')
    command.add_argument('--kind', metavar='NAME', help='Install into an existing kind cluster')
    command.set_defaults(run=install)
    args = parser.parse_args()
    try:
        args.run(args)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
