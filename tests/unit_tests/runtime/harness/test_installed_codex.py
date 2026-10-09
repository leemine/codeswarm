"""Installed Codex admission and credential isolation, using the real core policy."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from openjiuwen.harness.engine.config import config_fingerprint
from openjiuwen.harness_protocol import HarnessContext, HarnessRuntimePolicy, HostCapability
from openjiuwen.harness_providers.construction import compile_execution
from openjiuwen.harness_providers.codex.config import CodexHarnessConfig
from openjiuwen.harness_providers.codex.runtime_policy import compile_runtime_policy
from openjiuwen.harness_providers.codex.source_policy import validate_startup_sources

from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.config_source import load_execution_catalog
from jiuwenswarm.runtime.harness.installed_codex import (
    PROFILE, materialize_codex_source, prepare_codex_startup,
)
from jiuwenswarm.runtime.harness.request_binding import bind_admitted_request_execution
from jiuwenswarm.runtime.harness.recovery_store import ExecutionRecoveryUnavailableError


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv('JIUWENSWARM_ORGANIZATION_AUTH', raising=False)
    monkeypatch.setattr('jiuwenswarm.governance.organization_auth.configured_authenticator', lambda: None)
    internal, workspace, auth = [tmp_path / name for name in ('internal', 'project', 'login')]
    for path in (internal, workspace, auth):
        path.mkdir()
    (workspace / '.git').mkdir()
    (auth / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'test-login'}}))
    (auth / 'config.toml').write_text('notify=["untrusted-hook"]')
    monkeypatch.setenv('CODEX_HOME', str(auth))
    monkeypatch.setenv('UNRELATED_SECRET', 'must-not-inherit')
    source = load_execution_catalog({}, selected_profile_id=PROFILE).source()
    paths = RuntimeWorkspacePaths(internal, workspace, workspace, workspace)
    bound = materialize_codex_source(source, profile_id=PROFILE, paths=paths,
                                    subject_id='alice', session_id='s1')
    route = SimpleNamespace(bound=SimpleNamespace(spec=bound.resolve()), runtime_paths=paths,
                            recovery=SimpleNamespace(execution_profile_id=PROFILE))
    return source, paths, route, auth


def test_materialization_pins_only_admitted_scope_without_allocating(setup):
    source, paths, route, _ = setup
    config = route.bound.spec.provider_config
    assert config['startup_source_roots'] == (str(paths.runtime_workspace_root),
        config['env']['HOME'], config['env']['CODEX_HOME'])
    assert config['inherit_process_env'] is False
    assert 'UNRELATED_SECRET' not in config['env']
    assert not Path(config['env']['HOME']).exists()
    assert config_fingerprint(source.resolve()) != config_fingerprint(route.bound.spec)
    assert config_fingerprint(materialize_codex_source(source, profile_id=PROFILE, paths=paths,
        subject_id='alice', session_id='s1').resolve()) == config_fingerprint(route.bound.spec)
    other = materialize_codex_source(source, profile_id=PROFILE, paths=paths,
                                    subject_id='bob', session_id='s1')
    assert other.resolve().provider_config['env']['CODEX_HOME'] != config['env']['CODEX_HOME']
    assert materialize_codex_source(source, profile_id='custom', paths=paths,
                                   subject_id='alice', session_id='s1') is source
    with pytest.raises(ValueError, match='separate'):
        materialize_codex_source(source, profile_id=PROFILE,
            paths=replace(paths, runtime_workspace_root=paths.internal_workspace_dir),
            subject_id='alice', session_id='s1')


def test_startup_reuses_only_login_and_real_core_policy_accepts(setup):
    _, paths, route, auth = setup
    prepare_codex_startup(route)
    config = CodexHarnessConfig.from_mapping(compile_execution(route.bound.spec))
    home, codex = Path(config.env['HOME']), Path(config.env['CODEX_HOME'])
    import tomllib
    permission = tomllib.loads((codex / 'config.toml').read_text())
    assert 'notify' not in permission
    assert permission['default_permissions'] == 'codeswarm-session'
    assert permission['permissions']['codeswarm-session'] == {
        'filesystem': {':minimal': 'read', str(paths.runtime_workspace_root): 'write'},
        'network': {'enabled': False},
    }
    assert json.loads((codex / 'auth.json').read_text())['tokens']['access_token'] == 'test-login'
    assert (codex / 'auth.json').stat().st_mode & 0o777 == 0o600
    assert home.stat().st_mode & 0o777 == codex.stat().st_mode & 0o777 == 0o700
    policy = HarnessRuntimePolicy(revision='test-v1', surface='code', execution_state='normal', workspace_access='workspace_write')
    compiled = compile_runtime_policy(config, policy)
    context = HarnessContext(agent_name='test', agent_id='s1', host_session_id='s1', system_prompt='',
        cwd=str(paths.cwd), host_capabilities=frozenset({HostCapability.TOOL_APPROVAL}),
        interactions=SimpleNamespace())
    assert validate_startup_sources(compiled.config, context)
    # CLI-owned bundled skills remain in this Session's isolated home.
    (codex / 'skills').mkdir()
    (codex / 'skills/SKILL.md').write_text('Bundled skill')
    assert validate_startup_sources(compiled.config, context)
    (codex / 'auth.json').write_text('{"tokens":{"access_token":"refreshed"}}')
    prepare_codex_startup(route)
    assert 'refreshed' in (codex / 'auth.json').read_text()
    assert 'test-login' in (auth / 'auth.json').read_text()
    # An ambient project configuration still fails the existing source gate.
    (paths.cwd / '.codex').mkdir()
    (paths.cwd / '.codex/config.toml').write_text('notify=["untrusted-hook"]')
    with pytest.raises(Exception, match='configuration key'):
        validate_startup_sources(compiled.config, context)


@pytest.mark.parametrize('failure', ['missing', 'invalid', 'symlink', 'public_directory'])
def test_startup_rejects_missing_login_and_unsafe_destinations(setup, failure):
    _, _, route, auth = setup
    target = Path(route.bound.spec.provider_config['env']['CODEX_HOME'])
    if failure == 'missing':
        (auth / 'auth.json').unlink()
    elif failure == 'invalid':
        (auth / 'auth.json').write_text('not JSON')
    else:
        target.parent.parent.mkdir(mode=0o700)
        target.parent.mkdir(mode=0o700)
        if failure == 'symlink':
            target.symlink_to(auth, target_is_directory=True)
        else:
            target.mkdir(mode=0o755)
    with pytest.raises((RuntimeError, ValueError)):
        prepare_codex_startup(route)
    assert (auth / 'config.toml').read_text() == 'notify=["untrusted-hook"]'


def test_original_binding_recovery_pins_materialized_scope(setup, monkeypatch, tmp_path):
    source, paths, _, _ = setup
    from jiuwenswarm.runtime.harness import recovery_store
    monkeypatch.setattr('jiuwenswarm.common.config.get_config', lambda: {})
    monkeypatch.setattr('jiuwenswarm.common.utils.get_agent_workspace_dir', lambda: paths.internal_workspace_dir)
    monkeypatch.setattr(recovery_store, 'resolve_session_dir', lambda sid, create=False: (tmp_path/'sessions'/sid, None))
    monkeypatch.setattr(recovery_store, 'get_read_history_path', lambda sid: tmp_path/'sessions'/sid/'history.jsonl')
    request = SimpleNamespace(session_id='s1', channel_id='web', user_id='alice', params={})
    metadata = {'mode': 'agent.code.normal', 'work_mode': 'code', 'execution_profile_id': PROFILE,
        'execution_config_revision': source.resolve().config_revision,
        'execution_config_fingerprint': config_fingerprint(source.resolve())}
    def bind(project):
        return bind_admitted_request_execution(SimpleNamespace(execution_bindings=ExecutionBindingStore()),
            request, str(project), session_metadata=metadata)
    route = bind(paths.cwd)
    cold = bind(paths.cwd)
    assert cold.bound.binding == route.bound.binding
    assert route.bound.binding.fingerprint != metadata['execution_config_fingerprint']
    assert 'auth.json' not in route.recovery.path.read_text()
    other = tmp_path/'other'
    other.mkdir()
    with pytest.raises(ExecutionRecoveryUnavailableError):
        bind(other)
