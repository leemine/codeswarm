import assert from 'node:assert/strict';
import test from 'node:test';
import { parseTeamReviewExecution } from '../node_modules/.cache/team-review-execution/features/teamReviewExecution.js';
import { parseTeamHistoryPanelRecords } from '../node_modules/.cache/team-review-execution/features/teamHistoryPanelRestore.js';
import { parseHistoryJsonFileToTimelinePreview } from '../node_modules/.cache/team-review-execution/features/historyRestore.js';
import { buildProcessItems } from '../node_modules/.cache/team-review-execution/components/teamArea/shared.js';
import { useSessionStore } from '../node_modules/.cache/team-review-execution/stores/index.js';

const sid = 'review-session';
const row = (event_type, extra = {}) => ({
  id: 'root-goal:assistant',
  session_id: sid,
  mode: 'team.code.normal',
  role: 'reviewer',
  execution_kind: 'scheduled_review',
  member_name: 'same-name',
  review_task_id: 'task-a',
  review_round: 1,
  review_invocation_id: 'review-a',
  provider_id: 'codex',
  member_session_id: 'review-session:review-a',
  event_type,
  timestamp: 1700000000,
  content: 'Review output',
  tool_call: { tool_call_id: 'shared-call', name: 'verify_task', arguments: '{"task_id":"task-a"}' },
  tool_call_id: 'shared-call',
  tool_name: 'verify_task',
  result: 'accepted',
  ...extra,
});
const task = { id: 'task-a', title: 'Task A', status: 'completed' };
const parsed = (...rows) => rows.map((r) => parseTeamReviewExecution(r, sid));

test('live projected teammate and nested history retain the same review identity', () => {
  const live = parseTeamReviewExecution(row('chat.tool_call', { role: 'teammate' }), sid);
  const history = parseTeamReviewExecution({ event_type: 'chat.tool_call', event_payload: row('chat.tool_call') }, sid);
  assert.equal(live.id, history.id);
  assert.deepEqual(live.review, history.review);
  assert.equal(live.review.task_id, 'task-a');
});

test('review history never invents roster members or leaks into main chat/tools', () => {
  const records = ['chat.tool_call', 'chat.tool_result', 'chat.final'].map((type) => row(type));
  const state = parseTeamHistoryPanelRecords(records, sid);
  assert.equal(state.executionEvents.length, 3);
  assert.deepEqual(state.members, []);
  const timeline = parseHistoryJsonFileToTimelinePreview(records, sid);
  assert.deepEqual(timeline.messages, []);
  assert.deepEqual(timeline.executions, []);
  assert.equal(parseTeamHistoryPanelRecords(records, 'other-session').executionEvents.length, 0);
});

test('missing provenance is not presented as a same-name ordinary teammate', () => {
  for (const patch of [{ review_invocation_id: '' }, { review_task_id: '' }, { review_round: 0 }]) {
    const r = row('chat.final', { role: 'teammate', ...patch });
    assert.equal(parseTeamReviewExecution(r, sid), null);
    const state = parseTeamHistoryPanelRecords([r], sid);
    assert.deepEqual(state.members, []);
    assert.deepEqual(state.executionEvents, []);
  }
});

test('task association and invocation pairing isolate names, repeated calls and rounds', () => {
  const events = parsed(
    row('chat.tool_call'),
    row('chat.tool_result'),
    row('chat.final'),
    row('chat.tool_result', { review_invocation_id: 'review-b', review_round: 2, result: 'other review' }),
  );
  events.push({
    id: 'ordinary',
    member_id: 'same-name',
    kind: 'tool_result',
    tool_call_id: 'shared-call',
    content: 'ordinary result',
    timestamp: 1,
    title: '',
  });
  const items = buildProcessItems('worker', [task], [], [], events);
  assert.equal(items.length, 3);
  assert.equal(items.find((i) => i.kind === 'tool_call').linkedResult.content, 'accepted');
  assert.equal(items.filter((i) => i.kind === 'final').length, 1);
  assert.equal(items.find((i) => i.kind === 'tool_result').execution.content, 'other review');
  assert.deepEqual(buildProcessItems('same-name', [], [], [], events), []);
  assert.deepEqual(buildProcessItems('worker', [{ ...task, id: 'other' }], [], [], events), []);
});

test('store replay preserves distinct invocation results and late empty terminal keeps history text', () => {
  useSessionStore.getState().ensureRuntime(sid);
  const [a, b] = parsed(row('chat.final'), row('chat.final', { review_invocation_id: 'review-b' }));
  useSessionStore.getState().setTeamMemberExecutionEvents(sid, [a, b]);
  const terminal = parseTeamReviewExecution(
    row('team.member_turn', { content: '', terminal_status: 'completed' }),
    sid,
  );
  useSessionStore.getState().addTeamMemberExecutionEvent(sid, terminal);
  const events = useSessionStore.getState().getRuntime(sid).teamMemberExecutionEvents;
  assert.equal(events.length, 2);
  assert.equal(events.find((e) => e.id === a.id).content, 'Review output');
  assert.equal(events.find((e) => e.id === a.id).review.terminal_status, 'completed');
  useSessionStore.getState().setTeamMemberExecutionEvents(sid, [a, terminal, b]);
  assert.equal(useSessionStore.getState().getRuntime(sid).teamMemberExecutionEvents[0].content, 'Review output');
});

test('same-name ordinary member and review file provenance are preserved independently', () => {
  const ordinary = { ...row('chat.final'), execution_kind: undefined, role: 'teammate' };
  const file = row('chat.file', {
    files: [{ name: 'report.txt', artifact: { artifactId: 'artifact-a' }, delivery_id: 'delivery-a' }],
  });
  const state = parseTeamHistoryPanelRecords([ordinary, row('chat.final'), file], sid);
  assert.deepEqual(
    state.members.map((m) => m.member_id),
    ['same-name'],
  );
  assert.equal(state.executionEvents.filter((e) => !e.review).length, 1);
  assert.equal(state.executionEvents.find((e) => e.kind === 'file').files[0].artifact.artifactId, 'artifact-a');
});

for (const language of ['zh', 'en']) {
  test(`original process card renders review details in ${language}`, async () => {
    const { createElement } = await import('react');
    const { renderToStaticMarkup } = await import('react-dom/server');
    const { default: i18n } = await import('../node_modules/.cache/team-review-execution/i18n/index.js');
    const { ProcessListCard } =
      await import('../node_modules/.cache/team-review-execution/components/teamArea/ProcessListCard.js');
    await i18n.changeLanguage(language);
    const items = buildProcessItems('worker', [task], [], [], parsed(row('chat.tool_call'), row('chat.tool_result')));
    const html = renderToStaticMarkup(
      createElement(ProcessListCard, { items, expandedIds: new Set(items.map((i) => i.id)), onToggle() {} }),
    );
    for (const expected of [
      'same-name',
      'task-a',
      'review-a',
      'codex',
      'accepted',
      language === 'zh' ? '审查轮次' : 'Review round',
    ])
      assert.ok(html.includes(expected), expected);
  });
}

test('MCP structured history result keeps text and tool id rather than envelope id', () => {
  const value = parseTeamReviewExecution(
    row('chat.tool_result', {
      result: { content: [{ type: 'text', text: 'Vote recorded' }] },
    }),
    sid,
  );
  assert.equal(value.tool_call_id, 'shared-call');
  assert.equal(value.content, 'Vote recorded');
  assert.equal(
    parseTeamReviewExecution(
      row('chat.tool_call', { tool_call: { name: 'verify_task' }, tool_call_id: undefined }),
      sid,
    ),
    null,
  );
});
