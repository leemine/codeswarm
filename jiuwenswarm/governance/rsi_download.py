"""Experiment artifacts on the existing signed-token/chunk download transport."""
from dataclasses import asdict, dataclass
import os
from pathlib import Path
import time
from urllib.parse import urlencode

from .rsi_boundary import experiment_store, experiment_owner_check, is_instance_owner
from .workspace_download import (
    _open, _File, MAX_DOWNLOAD_TOKEN_BYTES, MAX_DOWNLOAD_CHUNK_BYTES,
    WorkspaceDownloadDenied,
)

NAMESPACE = 'rsi_artifact_v1'


def issue_experiment_download(path, task_id):
    from .session_boundary import current_application_permit
    from jiuwenswarm.agents.harness.common.tools.web_file_download import WebFileDownloadManager
    permit = current_application_permit('rsi.artifact.download')
    store = experiment_store()
    if not is_instance_owner(permit.identity) or not experiment_owner_check(task_id, permit.identity, store=store):
        raise WorkspaceDownloadDenied('Experiment artifact unavailable')
    root = str(store.task_dir(store.tasks_root, task_id).absolute())
    # Reuse the original no-symlink FD walk and file identity evidence.
    with _open(root, path) as (_, root_stamp, file_stamp):
        payload = {'path': path, 'sid': task_id, 'exp': int(time.time()) + 600,
                   NAMESPACE: {'identity': asdict(permit.identity), 'task_id': task_id,
                               'root': list(root_stamp), 'file': asdict(file_stamp),
                               'channel_id': 'web'}}
    if not permit.revalidate():
        raise WorkspaceDownloadDenied('Experiment authorization changed')
    token = WebFileDownloadManager.get_instance()._sign_payload(payload)
    if len(token.encode()) > MAX_DOWNLOAD_TOKEN_BYTES:
        raise WorkspaceDownloadDenied('Experiment selector too large')
    return {'download_token': token,
            'download_url': '/file-api/download?' + urlencode({'token': token, 'session_id': task_id})}


@dataclass(frozen=True, repr=False)
class ExperimentDownloadPermit:
    identity_resolver: object
    identity: object
    payload: dict
    token_check: object
    root: str
    store: object

    @property
    def size(self): return self.payload[NAMESPACE]['file']['size']
    @property
    def name(self): return Path(self.payload['path']).name
    @property
    def session_id(self): return self.payload['sid']
    @property
    def channel_id(self): return self.payload[NAMESPACE]['channel_id']

    @classmethod
    def capture(cls, identity_resolver, session_id, token, *, token_validator):
        from .contracts import TrustedIdentity
        if not isinstance(token, str) or not 0 < len(token.encode()) <= MAX_DOWNLOAD_TOKEN_BYTES:
            raise WorkspaceDownloadDenied('Experiment token unavailable')
        payload = token_validator(token, session_id=session_id)
        if type(payload) is not dict or set(payload) != {'path', 'sid', 'exp', NAMESPACE}:
            raise WorkspaceDownloadDenied('Experiment token unavailable')
        facts = payload[NAMESPACE]
        identity = identity_resolver()
        if (not isinstance(identity, TrustedIdentity) or type(facts) is not dict
                or set(facts) != {'identity', 'task_id', 'root', 'file', 'channel_id'}
                or facts['identity'] != asdict(identity) or facts['task_id'] != session_id
                or payload['sid'] != session_id or facts['channel_id'] != 'web'
                or type(payload['exp']) is not int
                or type(facts['root']) is not list or len(facts['root']) != 2
                or any(type(n) is not int or n < 0 for n in facts['root'])
                or type(facts['file']) is not dict or set(facts['file']) != set(_File.__dataclass_fields__)
                or any(type(n) is not int or n < 0 for n in facts['file'].values())):
            raise WorkspaceDownloadDenied('Experiment token unavailable')
        store = experiment_store()
        root = str(store.task_dir(store.tasks_root, session_id).absolute())
        def token_check():
            if token_validator(token, session_id=session_id) != payload:
                raise WorkspaceDownloadDenied('Experiment token expired')
        permit = cls(identity_resolver, identity, payload, token_check, root, store)
        permit.check()
        return permit

    def _authority(self):
        self.token_check()
        if (self.identity_resolver() != self.identity or not is_instance_owner(self.identity)
                or not experiment_owner_check(self.session_id, self.identity, store=self.store)):
            raise WorkspaceDownloadDenied('Experiment authorization changed')

    def _content_check(self, root, file):
        facts = self.payload[NAMESPACE]
        if list(root) != facts['root'] or asdict(file) != facts['file']:
            raise WorkspaceDownloadDenied('Experiment artifact changed')

    def check(self):
        self._authority()
        with _open(self.root, self.payload['path']) as (_, root, file):
            self._content_check(root, file)
        self._authority()

    def read(self, offset, limit):
        if (type(offset) is not int or not 0 <= offset <= self.size
                or type(limit) is not int or not 0 < limit <= MAX_DOWNLOAD_CHUNK_BYTES):
            raise WorkspaceDownloadDenied('Invalid experiment artifact range')
        self._authority()
        with _open(self.root, self.payload['path']) as (fd, root, file):
            self._content_check(root, file)
            self._authority()
            data = os.pread(fd, min(limit, self.size - offset), offset)
            self._content_check(root, _File.from_stat(os.fstat(fd)))
        self.check()
        if len(data) != min(limit, self.size - offset):
            raise WorkspaceDownloadDenied('Experiment artifact changed')
        return data
