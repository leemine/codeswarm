from copy import deepcopy
import pytest
from openjiuwen.harness.engine.config import config_fingerprint
from jiuwenswarm.governance.goal_read import capture_goal_read_route
from jiuwenswarm.governance.session_sharing import SessionSharingDenied
from jiuwenswarm.runtime.harness.config_source import load_execution_catalog


@pytest.mark.parametrize('change', ['action', 'profile'])
def test_first_owner_callback_cannot_retarget_prevalidated_goal_get(monkeypatch, change):
    from jiuwenswarm.common import config
    from jiuwenswarm.server.runtime.session import session_metadata
    cfg = {'execution': {'default_profile_id': 'native-a', 'profiles': {
        'native-a': {'provider_id': 'native', 'config_revision': 'r1', 'provider_config': {}},
        'native-b': {'provider_id': 'native', 'config_revision': 'r2', 'provider_config': {}},
    }}}
    catalog = load_execution_catalog(cfg)
    spec_a = catalog.source(explicit_profile_id='native-a').resolve()
    spec_b = catalog.source(explicit_profile_id='native-b').resolve()
    meta = {'channel_id': 'web', 'mode': 'agent.work.normal', 'work_mode': 'work',
            'project_id': 'project', 'execution_profile_id': 'native-a',
            'execution_config_revision': 'r1', 'execution_config_fingerprint': config_fingerprint(spec_a)}
    monkeypatch.setattr(config, 'get_config', lambda: cfg)
    monkeypatch.setattr(session_metadata, 'get_session_metadata', lambda *_a, **_kw: deepcopy(meta))
    params = {'action': 'get', 'session_id': 'owned'}
    calls = 0
    def check():
        nonlocal calls
        calls += 1
        if calls == 1:
            if change == 'action':
                params['action'] = 'clear'
            else:
                meta.update(execution_profile_id='native-b', execution_config_revision='r2',
                            execution_config_fingerprint=config_fingerprint(spec_b))
    with pytest.raises(SessionSharingDenied):
        capture_goal_read_route('owned', params, check)
