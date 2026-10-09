"""Storage subprocesses must not construct the unrelated agent/Rail graph."""
import subprocess
import sys


def test_permission_storage_import_does_not_load_team_or_agent_rails():
    result = subprocess.run([sys.executable, '-c', '''
import sys
from jiuwenswarm.agents.harness.common.rails.permissions import permissions_persist
assert callable(permissions_persist.persist_exact_permission_allow_rule)
assert 'jiuwenswarm.agents.harness.common.rails.symphony' not in sys.modules
assert 'jiuwenswarm.agents.harness.team.rails.team_member_skill_toolkit_rail' not in sys.modules
assert 'jiuwenswarm.server.runtime.agent_manager' not in sys.modules
'''], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_original_rail_exports_keep_identity_and_discovery():
    from jiuwenswarm.agents.harness.common import rails
    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
    from openjiuwen.harness.rails.security import PermissionInterruptRail
    assert rails.JiuSwarmStreamEventRail is JiuSwarmStreamEventRail
    assert rails.PermissionInterruptRail is PermissionInterruptRail
    assert set(rails.__all__) <= set(dir(rails))
    assert all(isinstance(getattr(rails, name), type) for name in rails.__all__)
