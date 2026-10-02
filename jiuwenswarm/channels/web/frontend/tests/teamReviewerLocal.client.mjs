// Opt-in real loopback WebSocket + existing React authorization/hook/history consumer.
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { join } from 'node:path';
import { build } from 'esbuild';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { I18nextProvider } from 'react-i18next';
import { JSDOM } from 'jsdom';
import WebSocket from 'ws';

const [origin, evidencePath] = process.argv.slice(2);
assert.equal(new URL(origin).hostname, '127.0.0.1');
const frontend = fileURLToPath(new URL('../', import.meta.url));
const cache = join(frontend, 'node_modules/.cache/team-reviewer-local');
await mkdir(cache, { recursive: true });
await build({
  absWorkingDir: frontend,
  entryPoints: [
    { in: 'src/hooks/useWebSocket.ts', out: 'hook' },
    { in: 'src/components/InteractionSlot/AuthorizationPrompt.tsx', out: 'prompt' },
    { in: 'src/stores/index.ts', out: 'stores' },
    { in: 'src/services/webClient.ts', out: 'client' },
    { in: 'src/features/teamHistoryPanelRestore.ts', out: 'history' },
    { in: 'src/i18n/index.ts', out: 'i18n' },
  ],
  bundle: true,
  splitting: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  loader: { '.css': 'empty' },
  define: { 'import.meta.env': '{"DEV":false}' },
  outdir: cache,
});
const dom = new JSDOM('<div id="root"></div>', { url: origin, pretendToBeVisual: true });
for (const [key, value] of Object.entries({
  window: dom.window,
  document: dom.window.document,
  navigator: dom.window.navigator,
  localStorage: dom.window.localStorage,
  HTMLElement: dom.window.HTMLElement,
  Node: dom.window.Node,
  CustomEvent: dom.window.CustomEvent,
  WebSocket,
  IS_REACT_ACT_ENVIRONMENT: true,
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  fetch: () => {
    throw new Error('Unexpected HTTP request');
  },
}))
  Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
const imported = async (name) => import(pathToFileURL(join(cache, name + '.js')).href);
const { useWebSocket } = await imported('hook');
const { AuthorizationPrompt } = await imported('prompt');
const { useChatStore, useSessionStore, useWorkspaceStore } = await imported('stores');
const { webClient } = await imported('client');
const { parseTeamHistoryPanelRecords } = await imported('history');
const { default: i18n } = await imported('i18n');
const sid = 'team-local';
useChatStore.getState().ensureRuntime(sid);
useChatStore.getState().setActiveSessionId(sid);
useSessionStore.getState().ensureRuntime(sid);
useSessionStore.getState().setMode(sid, 'team');
useWorkspaceStore.setState({ workMode: 'code' });
const seen = [],
  results = [],
  errors = [],
  frames = [];
let api,
  done = false;
function Host() {
  api = useWebSocket({ activeSessionId: sid, onError: (e) => errors.push(String(e)) });
  const pending = useChatStore((s) => s.runtimes[sid]?.pendingQuestions[0]);
  return pending
    ? createElement(AuthorizationPrompt, {
        pending,
        onSubmit: async (...args) => {
          const ok = await api.sendUserAnswer(sid, ...args);
          results.push(ok);
          return ok;
        },
      })
    : null;
}
const root = createRoot(document.getElementById('root'));
try {
  await act(async () => root.render(createElement(I18nextProvider, { i18n }, createElement(Host))));
  webClient.on('test.done', () => {
    done = true;
  });
  webClient.on('chat.ask_user_question', ({ payload }) => frames.push(payload));
  await webClient.request('test.ready', { session_id: sid, mode: 'team.code.normal' });
  const deadline = Date.now() + 100000;
  while (!done && Date.now() < deadline) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 15));
    });
    const pending = useChatStore.getState().getRuntime(sid)?.pendingQuestions[0];
    if (!pending || seen.some((p) => p.request_id === pending.request_id)) continue;
    assert.ok(pending.sessionGeneration > 0);
    assert.equal(pending.source, 'confirm_interrupt');
    const prompt = document.querySelector('[data-testid="interaction-slot-auth-prompt"]');
    assert.ok(prompt);
    const button = prompt.querySelector('[data-variant="allow-once"]');
    assert.ok(button && !button.disabled);
    seen.push(pending);
    await act(async () => button.click());
  }
  await writeFile(
    evidencePath,
    JSON.stringify({ seen, results, errors, frames, runtime: useSessionStore.getState().getRuntime(sid) }, null, 2),
  );
  assert.ok(done, 'Goal must settle before UI timeout');
  await act(async () => {
    await new Promise((r) => setTimeout(r, 30));
  });
  assert.ok(seen.length >= 3);
  assert.equal(results.length, seen.length);
  assert.ok(results.every(Boolean));
  assert.equal(document.querySelector('[data-testid="interaction-slot-auth-prompt"]'), null);
  assert.deepEqual(errors, []);
  const raw = await webClient.request('team.history.get', { session_id: sid, limit: 500, max_bytes: 1024 * 1024 });
  assert.ok(raw.records.length);
  const restored = parseTeamHistoryPanelRecords(raw.records, sid);
  await writeFile(evidencePath, JSON.stringify({ seen, results, errors, frames, raw, restored }, null, 2));
  // This scheduled scenario sends no inter-member chat. Restore the actual
  // task board and worker execution records instead of inventing messages.
  assert.ok(restored.tasks.some((task) => task.task_id === 'reviewed' && task.status === 'completed'));
  assert.ok(restored.executionEvents.some((event) => event.member_id === 'worker' && event.kind === 'final'));
  assert.ok(restored.members.some((member) => member.member_id === 'worker'));
  assert.ok(!restored.members.some((member) => member.member_id === 'reviewer'));
  const reviews = raw.records.filter((record) => record.execution_kind === 'scheduled_review');
  assert.ok(reviews.some((record) => record.event_type === 'chat.tool_result'));
  assert.ok(
    reviews.some(
      (record) => record.event_type === 'chat.final' && record.terminal_status === 'completed' && record.content.trim(),
    ),
  );
  assert.ok(
    reviews.every(
      (record) =>
        record.role === 'reviewer' &&
        record.review_task_id === 'reviewed' &&
        record.review_round === 1 &&
        record.review_invocation_id,
    ),
  );
} finally {
  await act(async () => root.unmount());
  await webClient.disconnect();
  dom.window.close();
}
