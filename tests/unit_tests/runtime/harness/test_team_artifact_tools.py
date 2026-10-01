"""Member file delivery preserves workspace, Turn and artifact identity."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.runtime.harness.team_projection import TeamMemberProjection


def projection(tmp_path):
    value = object.__new__(TeamMemberProjection)
    value.route = SimpleNamespace(runtime_paths=SimpleNamespace(runtime_workspace_root=tmp_path), provider_id='codex')
    value.binding = SimpleNamespace(host_session_id='root:member:worker')
    value._active_turn_id = 'member-turn'
    value.artifact = AsyncMock()
    return value


@pytest.mark.asyncio
async def test_file_delivery_uses_member_turn_and_stable_artifact_identity(tmp_path):
    host = projection(tmp_path)
    file = tmp_path / 'result.txt'
    file.write_text('42')
    tool = host.artifact_tools()[0]
    await tool.invoke({'abs_file_path_list': str(file)})
    await tool.invoke({'abs_file_path_list': str(file)})
    first, second = [call.args[0] for call in host.artifact.await_args_list]
    assert first.artifactId == second.artifactId
    assert first.metadata['session_id'] == 'root:member:worker'
    assert first.metadata['turn_id'] == 'member-turn'
    assert first.metadata['workspace_relative_path'] == 'result.txt'


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['outside', 'symlink', 'relative', 'inactive', 'batch'])
async def test_file_delivery_rejects_unowned_paths_before_any_delivery(tmp_path, case):
    import json
    root = tmp_path / 'workspace'
    root.mkdir()
    host = projection(root)
    outside = tmp_path / 'private.txt'
    outside.write_text('not a deliverable')
    inside = root / 'result.txt'
    inside.write_text('42')
    value = str(outside)
    if case == 'symlink':
        link = root / 'link.txt'
        link.symlink_to(outside)
        value = str(link)
    elif case == 'relative':
        value = 'result.txt'
    elif case == 'inactive':
        host._active_turn_id = None
        value = str(inside)
    elif case == 'batch':
        value = json.dumps([str(inside), str(outside)])
    with pytest.raises((ValueError, RuntimeError)):
        await host.artifact_tools()[0].invoke({'abs_file_path_list': value})
    host.artifact.assert_not_awaited()
