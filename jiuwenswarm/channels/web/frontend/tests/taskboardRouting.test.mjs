import test from 'node:test';
import assert from 'node:assert/strict';
import { parseChatRoute, chatRoutePath } from '../node_modules/.cache/taskboard-routing/route.mjs';

test('taskboard deep links round-trip and preserve existing chat routes', () => {
  const routes = [
    { kind: 'taskboard' },
    { kind: 'taskboard', taskId: '550e8400-e29b-41d4-a716-446655440000' },
    { kind: 'chat-session', sessionId: 'web_hello' },
    { kind: 'chat-new' },
  ];
  for (const route of routes) assert.deepEqual(parseChatRoute(chatRoutePath(route)), route);
  assert.deepEqual(parseChatRoute('/taskboard/'), { kind: 'taskboard' });
  assert.deepEqual(parseChatRoute('/chat'), { kind: 'chat-new' });
});
test('invalid task paths do not become a chat session or silently open a board', () => {
  for (const route of ['/taskboard/../../x', '/taskboard/invalid', '/taskboard/123/extra'])
    assert.equal(parseChatRoute(route), null);
});
