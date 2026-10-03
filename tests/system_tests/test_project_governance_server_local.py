"""Real AgentServer listener/Runtime lifecycle with persistent local project ACL.

The worker owns both loopback listeners and all state under pytest tmp_path.
Identity switching is a trusted host test seam, not remote-user authentication.
No model, WebSocket server, Runtime, or product startup service is mocked.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.system]
_REPO = Path(__file__).resolve().parents[2]


async def _worker(report_path: Path) -> None:
    # Optional Hub preload is directed to an owned local unavailable endpoint,
    # so this ACL/lifecycle canary never depends on an external catalog service.
    upstream_calls = 0
    async def unavailable_catalog(reader, writer):
        nonlocal upstream_calls
        try:
            await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
            upstream_calls += 1
            writer.write(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    catalog = await asyncio.start_server(unavailable_catalog, '127.0.0.1', 0)
    catalog_port = catalog.sockets[0].getsockname()[1]
    os.environ['TEAM_SKILLS_HUB_BASE_URL'] = f'http://127.0.0.1:{catalog_port}'

    import importlib.metadata
    import importlib.util
    from websockets.asyncio.client import connect
    from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary
    from jiuwenswarm.common.utils import get_agent_root_dir, get_config_file
    from jiuwenswarm.governance.contracts import TrustedIdentity
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore

    server = AgentWebSocketServer(host='127.0.0.1', port=0)
    owner = server._resolve_trusted_identity(None)
    assert owner is not None and owner.authority == 'local-single-user-installation'
    assert get_config_file().is_relative_to(Path(os.environ['JIUWENSWARM_DATA_DIR']))
    first_runtime = server.get_runtime()
    observations = []
    listeners = []
    uri = ''

    async def start():
        nonlocal uri
        await asyncio.wait_for(server.start(), 25)
        listener = server._server
        listeners.append(listener)
        uri = f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}'
        await asyncio.wait_for(asyncio.shield(server._checkpointer_warmup_task), 30)
        assert server.get_runtime().started

    async def rpc(method, **params):
        request_id = f'live-{len(observations)}'
        async with connect(uri, open_timeout=5, close_timeout=5) as ws:
            ack = json.loads(await asyncio.wait_for(ws.recv(), 10))
            assert ack.get('event') == 'connection.ack'
            await ws.send(json.dumps({
                'request_id': request_id, 'channel_id': 'web', 'req_method': method,
                'user_id': owner.actor_id, 'params': params,
                'metadata': {'actor_id': owner.actor_id, 'authority': 'forged-wire'},
            }))
            async with asyncio.timeout(15):
                while True:
                    wire = json.loads(await ws.recv())
                    if wire.get('type') == 'event':
                        continue
                    response = parse_agent_server_wire_unary(wire)
                    if response.request_id == request_id:
                        break
            observations.append({'method':method,'ok':response.ok,'code':(response.payload or {}).get('code')})
            return response

    async def stop():
        await asyncio.wait_for(server.stop(), 25)
        assert server._server is None
        for name in ('_checkpointer_warmup_task', '_mcp_prewarm_task',
                     '_login_credential_refresh_task', '_personal_context_start_task',
                     '_asset_start_task', '_archive_service'):
            assert getattr(server, name) is None
        assert not server._tokenizer_warmup_tasks
        assert not server._session_stream_tasks

    try:
        await start()
        workspace = report_path.parent / 'project-workspace'
        workspace.mkdir()
        created = await rpc('project.create', name='Real listener', project_dir=str(workspace), work_mode='work')
        assert created.ok, created.payload
        project_id = created.payload['project_id']
        stored = ProjectAccessStore().get(project_id, owner.actor_id)
        assert stored['owner_id'] == owner.actor_id
        assert stored['acl_revision'] == 1
        granted = await rpc('project.acl.update', project_id=project_id,
                            acl={'reader':['read']}, expected_revision=1)
        assert granted.ok and granted.payload['acl_revision'] == 2

        # Switch only a host-owned resolver. Wire user_id/metadata remain forged.
        current = TrustedIdentity('reader','reader','test-host-resolver')
        server._trusted_identity_resolver = lambda _request: current
        assert (await rpc('project.info', project_id=project_id)).ok
        rejected = await rpc('project.rename', project_id=project_id, name='Denied')
        assert not rejected.ok and rejected.payload['code'] == 'FORBIDDEN'
        current = owner
        revoked = await rpc('project.acl.update', project_id=project_id, acl={}, expected_revision=2)
        assert revoked.ok and revoked.payload['acl_revision'] == 3
        current = TrustedIdentity('reader','reader','test-host-resolver')
        rejected = await rpc('project.info', project_id=project_id)
        assert not rejected.ok and rejected.payload['code'] == 'FORBIDDEN'

        await stop()
        assert first_runtime.closed
        recovered = server.get_runtime()
        assert recovered is not first_runtime
        assert not listeners[0].is_serving()
        await start()
        assert server.get_runtime() is recovered
        rejected = await rpc('project.info', project_id=project_id)
        assert not rejected.ok and rejected.payload['code'] == 'FORBIDDEN'
        current = owner
        final = await rpc('project.extensions.get', project_id=project_id)
        assert final.ok and final.payload['acl_revision'] == 3
        assert final.payload['acl'] == {}
        await stop()
        assert recovered.closed and server.get_runtime() is not recovered
        assert all(not listener.is_serving() for listener in listeners)
        report_path.write_text(json.dumps({
            'kind':'real-AgentWebSocketServer-start-stop-restart',
            'identity':'default-loopback-installation-then-host-injected-test-actor',
            'remote_auth':'not_tested', 'model_execution':'not_tested',
            'runtime_started_twice':True, 'runtime_replaced_twice':True,
            'persistent_acl_revision':3, 'listeners_closed':True,
            'optional_catalog':'owned-loopback-503', 'catalog_calls':upstream_calls,
            'agent_root':str(get_agent_root_dir()), 'operations':observations,
            'python':sys.executable,
            'core_import':importlib.util.find_spec('openjiuwen').origin,
            'core_source':json.loads(importlib.metadata.distribution('openjiuwen').read_text('direct_url.json')),
            'swarm_import':importlib.util.find_spec('jiuwenswarm').origin,
        },indent=2)+'\n')
    finally:
        if server._server is not None:
            await asyncio.wait_for(server.stop(), 25)
        catalog.close()
        await catalog.wait_closed()


def test_real_agent_server_project_acl_restart_and_cleanup(tmp_path):
    home, data = tmp_path / 'home', tmp_path / 'data'
    home.mkdir()
    config = data / 'config'
    config.mkdir(parents=True)
    # Omit startup_mode: the production boot contract explicitly selects host
    # mode when it is absent. No jiuwenbox child process or model is configured.
    (config / 'config.yaml').write_text(json.dumps({
        'models':{'defaults':[]}, 'sandbox':{'enabled':False}, 'mcp':{},
        'context_engine':{'token_counter':{'enabled':False}},
    }))
    (config / '.env').write_text('')
    report = tmp_path / 'real-server-report.json'
    env = os.environ.copy()
    for name in ('API_KEY','API_BASE','MODEL_NAME','MODEL_PROVIDER','MODEL_ALIAS',
                 'TEAM_SKILLS_HUB_SYSTEM_TOKEN','TEAM_SKILLS_HUB_USER_TOKEN'):
        env.pop(name, None)
    env.update({'JIUWENSWARM_HOME':str(home), 'JIUWENSWARM_DATA_DIR':str(data),
                'JIUWENSWARM_CONFIG_URL':'off', 'PYTHONPATH':str(_REPO),
                'XDG_CACHE_HOME':str(tmp_path/'cache'), 'HF_HOME':str(tmp_path/'hf'),
                'NO_PROXY':'127.0.0.1,localhost', 'no_proxy':'127.0.0.1,localhost'})
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker', str(report)],
                            cwd=_REPO, env=env, capture_output=True, text=True, timeout=110)
    evidence = os.getenv('R1_THREE_EVIDENCE_DIR')
    if evidence:
        target = Path(evidence)
        target.mkdir(parents=True, exist_ok=True)
        (target/'project-real-server-worker.log').write_text(result.stdout+'\n'+result.stderr)
        if report.exists():
            (target/'project-real-server.json').write_text(report.read_text())
    assert result.returncode == 0, result.stdout+'\n'+result.stderr
    observed = json.loads(report.read_text())
    assert observed['listeners_closed'] and observed['runtime_replaced_twice']
    assert observed['persistent_acl_revision'] == 3


if __name__ == '__main__' and sys.argv[1:2] == ['--worker']:
    asyncio.run(_worker(Path(sys.argv[2])))
