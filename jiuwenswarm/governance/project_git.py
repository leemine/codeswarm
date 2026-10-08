"""Request-local Git authorization over the existing project/resource stores.

This is not an execution mode or a second Git service. Ordinary standalone
repositories are supported; linked/parent repositories and external push
credentials need their own explicit resource authority and remain closed.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from .contracts import TrustedIdentity
from .resources import ResourceGuard, ResourceRequest
from .session_sharing import SessionSharingDenied

GIT_METHODS = frozenset('project.git.' + name for name in (
    'status', 'probe', 'init', 'switch_branch', 'create_branch', 'commit', 'push',
))
READ_METHODS = frozenset({'project.git.status', 'project.git.probe'})
_active = ContextVar('authorized_project_git', default=None)


def _deny(message='Git project/resource authorization required'):
    raise SessionSharingDenied(message)


def _environment(root):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT='0', GIT_CEILING_DIRECTORIES=str(Path(root).parent))
    return env


def _configuration(root, *, bare=False):
    gitdir = root if bare else root / '.git'
    if not gitdir.exists() and not gitdir.is_symlink():
        return {}
    if not gitdir.is_dir() or gitdir.is_symlink():
        _deny('Linked Git directories require separate resource authorization')
    for relative in ('config', 'HEAD', 'index', 'objects', 'refs', 'logs', 'hooks', 'packed-refs', 'objects/info'):
        path = gitdir / relative
        if path.is_symlink() or not path.resolve().is_relative_to(gitdir.resolve()):
            _deny('Git metadata must remain inside its authorized workspace')
    for directory, dirs, files in os.walk(gitdir, followlinks=False):
        if any((Path(directory) / name).is_symlink() for name in dirs + files):
            _deny('Git metadata symlinks require separate resource authorization')
    if any((gitdir / name).exists() for name in ('commondir', 'worktrees', 'objects/info/alternates')):
        _deny('Shared Git storage requires separate resource authorization')
    config = gitdir / 'config'
    if not config.exists():
        return {}
    executable = shutil.which('git')
    if not executable:
        _deny('Git is unavailable')
    result = subprocess.run([executable, 'config', '--file', str(config), '--no-includes', '--null', '--list'],
                            env=_environment(root), capture_output=True, text=True, timeout=10, check=False)
    if result.returncode:
        _deny('Git configuration is unavailable')
    values = {}
    for entry in result.stdout.split('\0'):
        if not entry:
            continue
        key, _, value = entry.partition('\n')
        key = key.lower()
        if (key.startswith(('include.', 'includeif.', 'filter.', 'url.'))
                or key in {'core.worktree', 'core.sshcommand', 'core.gitproxy', 'extensions.partialclone'}
                or key.endswith('.promisor')
                or key.endswith(('.textconv', '.command', '.receivepack', '.uploadpack'))):
            _deny('Git executable configuration requires separate authorization')
        values.setdefault(key, []).append(value)
    return values


@dataclass(frozen=True)
class GitRequest:
    method: str
    params_json: str
    project_id: str
    root: str
    inode: tuple
    identity: TrustedIdentity
    resolver: object
    store: object
    acl_revision: int
    resource_revision: int

    def check(self):
        from jiuwenswarm.server.runtime.session import project_store
        if self.resolver() != self.identity:
            _deny()
        project = project_store.get_project_by_id(self.project_id, cache_bust=True)
        if not project or project.work_mode != 'code' or project.project_dir != self.root:
            _deny()
        root = Path(self.root)
        if str(root.resolve()) != self.root or (root.stat().st_dev, root.stat().st_ino) != self.inode:
            _deny()
        action = 'read' if self.method in READ_METHODS else 'write'
        acl = self.store.authorize(self.project_id, self.identity.actor_id, action)
        if not acl.allowed or acl.revision != self.acl_revision:
            _deny()
        for operation in ({'read'} if action == 'read' else {'read', 'write'}):
            decision = ResourceGuard(self.store).check(self.project_id, self.identity,
                ResourceRequest('workspace', operation, self.root))
            if decision.resource_revision != self.resource_revision:
                _deny()

    @contextmanager
    def consume(self, method, params):
        if method != self.method or json.dumps(params, sort_keys=True) != self.params_json:
            _deny()
        with self.store._locked():
            self.check()
            root = Path(self.root)
            # Do not allow Git to discover a parent repository outside the selection.
            if not (root / '.git').exists() and any((p / '.git').exists() for p in root.parents):
                _deny('Select and authorize the Git repository root')
            config = _configuration(root)
            remote = None
            if method == 'project.git.push':
                name = params.get('remote', 'origin')
                if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name):
                    _deny()
                urls = config.get(f'remote.{name.lower()}.pushurl', config.get(f'remote.{name.lower()}.url', []))
                if len(urls) != 1 or not Path(urls[0]).is_absolute():
                    _deny('External Git push requires remote credential authorization')
                remote = Path(urls[0])
                if str(remote.resolve()) != str(remote) or not remote.is_dir():
                    _deny()
                ResourceGuard(self.store).check(self.project_id, self.identity,
                    ResourceRequest('workspace', 'write', str(remote)))
                if not (remote / 'HEAD').is_file() or not (remote / 'objects').is_dir():
                    _deny('Only an authorized local bare remote is available')
                remote_config = _configuration(remote, bare=True)
                hook_dir = remote_config.get('core.hookspath', [str(remote / 'hooks')])[-1]
                hook_root = Path(hook_dir)
                if not hook_root.is_absolute():
                    hook_root = remote / hook_root
                if hook_root != remote / 'hooks' or any(p.is_file() and not p.name.endswith('.sample') and os.access(p, os.X_OK)
                                                       for p in hook_root.glob('*')):
                    _deny('Remote hooks require separate process authorization')
            token = _active.set(self)
            try:
                yield
                self.check()
            finally:
                _active.reset(token)


def capture_git_request(method, params, identity_resolver, store):
    from jiuwenswarm.server.runtime.session import project_store
    identity = identity_resolver()
    project_id = params.get('project_id')
    if method not in GIT_METHODS or not isinstance(identity, TrustedIdentity) or not isinstance(project_id, str):
        _deny()
    project = project_store.get_project_by_id(project_id, cache_bust=True)
    if not project or project.work_mode != 'code':
        _deny()
    root = Path(project.project_dir)
    if not root.is_absolute() or str(root.resolve()) != str(root) or not root.is_dir():
        _deny()
    action = 'read' if method in READ_METHODS else 'write'
    acl = store.authorize(project_id, identity.actor_id, action)
    resource = ResourceGuard(store).check(project_id, identity, ResourceRequest('workspace', 'read', str(root)))
    request = GitRequest(method, json.dumps(params, sort_keys=True), project_id, str(root),
        (root.stat().st_dev, root.stat().st_ino), identity, identity_resolver, store, acl.revision, resource.resource_revision)
    request.check()
    return request


def git_command_context(cwd):
    request = _active.get()
    if request is None:
        return [], None
    request.check()
    if str(Path(cwd).resolve()) != request.root:
        _deny()
    args = []
    for value in ('core.hooksPath=' + os.devnull, 'core.fsmonitor=false', 'submodule.recurse=false',
                  'commit.gpgSign=false', 'tag.gpgSign=false', 'gc.auto=0', 'maintenance.auto=false'):
        args.extend(['-c', value])
    return args, _environment(request.root)
