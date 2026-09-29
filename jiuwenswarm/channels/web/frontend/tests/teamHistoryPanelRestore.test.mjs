import assert from 'node:assert/strict';
import test from 'node:test';

import { parseTeamHistoryPanelRecords } from '../node_modules/.cache/team-history-panel/teamHistoryPanelRestore.mjs';

function spawnRecord() {
  return {
    mode: 'team',
    event_type: 'chat.tool_call',
    timestamp: 1_700_000_000_000,
    tool_call: { name: 'spawn_member', arguments: JSON.stringify({ member_name: 'dev-1' }) },
  };
}

function shutdownResultRecord(fields) {
  return {
    mode: 'team',
    event_type: 'chat.tool_result',
    timestamp: 1_700_000_001_000,
    tool_name: 'shutdown_member',
    tool_call_id: 'call-1',
    ...fields,
  };
}

test('member shutdown is read from rendered_result, the text the model read', () => {
  const state = parseTeamHistoryPanelRecords(
    [
      spawnRecord(),
      shutdownResultRecord({
        result: "success=True data={'member_name': 'dev-1'} error=None",
        rendered_result: 'Member shutdown: member_name=dev-1',
      }),
    ],
    'session-1',
  );

  assert.deepEqual(state.members.map((member) => member.member_id), []);
});

test('records without rendered_result still parse the compatibility result', () => {
  const state = parseTeamHistoryPanelRecords(
    [spawnRecord(), shutdownResultRecord({ result: 'Member shutdown: member_name=dev-1' })],
    'session-1',
  );

  assert.deepEqual(state.members.map((member) => member.member_id), []);
});

test('Artifact history preserves IDs across replay and keeps same-path versions', () => {
  const records = ['a', 'a', 'b'].map((artifactId, index) => ({
    id: `file-${index}`, role: 'teammate', event_type: 'chat.file',
    session_id: 'session-1', member_id: 'browser', timestamp: 1700000000 + index,
    files: [{ name: 'report.pdf', path: '/outputs/report.pdf', size: 42,
      delivery_id: `browser-artifact:${artifactId}`, artifact: { artifactId } }],
  }));
  const state = parseTeamHistoryPanelRecords(records, 'session-1');
  const events = state.executionEvents.filter((event) => event.kind === 'file');
  assert.equal(events.length, 2);
  assert.deepEqual(events.flatMap((event) => event.files.map((file) => file.artifact.artifactId)), ['a', 'b']);
});
