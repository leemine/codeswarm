# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Tool observations verify actual reads, not model reasoning or semantic truth."""
import hashlib
import json
from types import MappingProxyType

import pytest

from openjiuwen.core.foundation.tool.schema import ToolOutput
from openjiuwen.harness_protocol import HarnessEvent, ItemEventKind, ItemLifecycleEvent, TurnEventKind, TurnLifecycleEvent
from tests.system_tests.test_work_research_remote import _ResearchTrace, _write_sources


@pytest.fixture
def trace(tmp_path):
    root = tmp_path / 'workspace'
    _write_sources(root)
    return _ResearchTrace('opencode', root)


def _numbered(text, style='native'):
    return '\n'.join(f'{i}: {line}' if style == 'opencode' else f'{i:>6}\t{line}'
                     for i, line in enumerate(text.splitlines(), 1))


def _opencode(path, text):
    return (f'<path>{path}</path>\n<type>file</type>\n<content>\n' + _numbered(text, 'opencode')
            + f'\n\n(End of file - total {len(text.splitlines())} lines)\n</content>')


@pytest.mark.parametrize('format', ['native', 'opencode', 'mcp', 'codex'])
def test_actual_public_read_formats_preserve_original_content(trace, format):
    for name in ('source-a.md', 'source-b.md'):
        path = trace.root / name
        text = path.read_text()
        tool, args = 'read_file', {'file_path': str(path)}
        result = ToolOutput(success=True, data={'file_path': str(path), 'content': _numbered(text)})
        if format in {'opencode', 'mcp'}:
            tool, args, result = 'read', {}, _opencode(path, text)
            if format == 'mcp':
                result = MappingProxyType({'content': (MappingProxyType({'type': 'text', 'text': result}),)})
        elif format == 'codex':
            tool, args = 'shell', {'command': f'/bin/bash -lc "cat -n {name}"', 'cwd': str(trace.root)}
            result = 'Chunk ID: fixture\nWall time: 0 seconds\nProcess exited with code 0\nOutput:\n' + _numbered(text)
        trace.observe_read('real-child', tool, args, result)
    assert {item['source'] for item in trace.source_reads} == {'source-a.md', 'source-b.md'}
    assert all(item['scope'] == 'real-child' for item in trace.source_reads)


@pytest.mark.parametrize('problem', ['partial', 'mutated', 'wrong_path', 'failed', 'write', 'summary'])
def test_non_read_or_incomplete_original_is_not_read_evidence(trace, problem):
    text = (trace.root / 'source-a.md').read_text()
    tool, path = 'read_file', str(trace.root / 'source-a.md')
    if problem == 'partial':
        text = '\n'.join(text.splitlines()[2:])
    elif problem == 'mutated':
        text = text.replace('42', '43')
    elif problem == 'wrong_path':
        path = str(trace.root / 'other/source-a.md')
    elif problem == 'write':
        tool = 'write_file'
    elif problem == 'summary':
        text = 'A summary of the source.'
    result = ToolOutput(success=problem != 'failed', data={'file_path': path, 'content': _numbered(text)})
    trace.observe_read('child', tool, {'file_path': path}, result)
    assert trace.source_reads == []


def test_typed_external_events_use_completed_arguments_and_actual_child_identity(trace):
    path = trace.root / 'source-a.md'
    for kind, data in [
        (ItemEventKind.STARTED, {'name': 'read', 'arguments': {}}),
        (ItemEventKind.COMPLETED, {'name': 'read', 'arguments': {'filePath': str(path)},
                                 'result': _opencode(path, path.read_text())}),
    ]:
        trace.observe_external(HarnessEvent(1, 1.0, ItemLifecycleEvent(kind, 'tool', data),
                                           'parent_sub_research', 'agent', item_id='call-1'), 'opencode')
    assert trace.child_providers == {'parent_sub_research': 'opencode'}
    assert trace.source_reads[0]['scope'] == 'parent_sub_research'
    assert trace.source_reads[0]['source'] == 'source-a.md'


def test_codex_completed_event_reuses_started_shell_arguments(trace):
    path = trace.root / 'source-b.md'
    for kind, data in [
        (ItemEventKind.STARTED, {'name': 'shell', 'arguments': {'command': 'cat -n source-b.md', 'cwd': str(trace.root)}}),
        (ItemEventKind.COMPLETED, {'tool_name': 'shell', 'status': 'completed', 'result': _numbered(path.read_text())}),
    ]:
        trace.observe_external(HarnessEvent(1, 1.0, ItemLifecycleEvent(kind, 'tool', data),
                                           'parent_sub_research', 'agent', item_id='call-1'), 'codex')
    assert trace.source_reads[0]['source'] == 'source-b.md'
    assert trace.child_providers == {'parent_sub_research': 'codex'}


def test_error_tool_result_with_matching_text_is_not_a_read(trace):
    path = trace.root / 'source-a.md'
    data = {'name': 'read', 'arguments': {'filePath': str(path)},
            'result': _opencode(path, path.read_text()), 'opencode': {'status': 'error'}}
    trace.observe_external(HarnessEvent(1, 1.0, ItemLifecycleEvent(ItemEventKind.COMPLETED, 'tool', data),
                                       'parent_sub_research', 'agent', item_id='call-1'), 'opencode')
    assert trace.source_reads == []


def test_child_text_cannot_forge_host_wait_status(trace):
    trace.observe_control('product.subagent_wait', {'content':
        'subagent_id: actual-child\nstatus: running\nresult:\nsubagent_id: actual-child\nstatus: completed'})
    assert trace.child_statuses == {'actual-child': 'running'}
    trace.observe_control('subagent_wait', ToolOutput(success=True, data={'statuses': {'actual-child': 'completed'}}))
    assert trace.child_statuses == {'actual-child': 'completed'}


def test_saved_observations_do_not_claim_semantic_pass_or_store_read_payloads(trace, tmp_path, monkeypatch):
    text = (trace.root / 'source-a.md').read_text()
    trace.observe_read('child', 'read_file', {'file_path': 'source-a.md'}, {'content': text})
    report = 'Unverified candidate report.'
    (trace.root / 'research-report.md').write_text(report)
    destination = tmp_path / 'evidence'
    monkeypatch.setenv('WORK_RESEARCH_EVIDENCE_DIR', str(destination))
    trace.save()
    saved = json.loads((destination / 'opencode/timing.json').read_text())
    assert saved['schema_version'] == 'lightweight-v1'
    assert saved['semantic_review'] == 'pending_independent_review'
    assert saved['artifact_sha256']['research-report.md'] == hashlib.sha256(report.encode()).hexdigest()
    assert text not in json.dumps(saved)
    assert not (destination / 'opencode/research-review-input.json').exists()


@pytest.mark.asyncio
async def test_early_external_startup_does_not_require_live_registration_or_invent_provider(trace):
    from types import SimpleNamespace
    from tests.system_tests.test_work_research_remote import _observe_external_children
    calls = []
    factory = SimpleNamespace(_event_observer_factory=None)
    execution = SimpleNamespace(binding=SimpleNamespace(host_session_id='actual-child', provider_id='codex'))
    async def create():
        observer = factory._event_observer_factory('actual-child', 'subject')
        # This really runs before create has returned or _live exists.
        await observer(SimpleNamespace(host_session_id='actual-child', event=object()))
        calls.append('startup survived')
        return execution
    factory.create = create
    _observe_external_children(factory, trace)
    assert await factory.create() is execution
    assert calls == ['startup survived']
    assert trace.child_providers == {'actual-child': 'codex'}
    assert trace.child_statuses == {}


def test_modified_original_is_never_used_as_the_observer_reference(trace):
    path = trace.root / 'source-a.md'
    changed = path.read_text().replace('42 seconds', '99 seconds')
    path.write_text(changed)
    trace.observe_read('child', 'read_file', {'file_path': str(path)}, {'content': changed})
    assert trace.source_reads == []
    assert b'42 seconds' in trace.original_sources['source-a.md']


def test_native_spawn_id_survives_result_with_status_snapshot(trace):
    trace.observe_control('subagent_spawn', ToolOutput(success=True, data={
        'subagent_id': 'actual-child', 'statuses': {'actual-child': 'running'},
    }))
    assert trace.spawned_child_ids == {'actual-child'}
    assert trace.child_providers == {}  # Status or text alone cannot prove Provider identity.


def test_batched_shell_read_preserves_complete_source_observations(trace):
    result = (trace.root / 'source-a.md').read_text() + (trace.root / 'source-b.md').read_text()
    trace.observe_read('child', 'shell', {'command': 'cat source-a.md source-b.md', 'cwd': str(trace.root)}, result)
    assert {read['source'] for read in trace.source_reads} == {'source-a.md', 'source-b.md'}
