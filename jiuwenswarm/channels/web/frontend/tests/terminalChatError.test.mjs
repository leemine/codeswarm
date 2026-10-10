import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdir } from 'node:fs/promises';
import { build } from 'esbuild';
import { JSDOM } from 'jsdom';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';

const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
for (const [key, value] of Object.entries({
  window: dom.window, document: dom.window.document,
  localStorage: dom.window.localStorage, sessionStorage: dom.window.sessionStorage,
  navigator: dom.window.navigator, Event: dom.window.Event, CustomEvent: dom.window.CustomEvent,
  IS_REACT_ACT_ENVIRONMENT: true,
})) Object.defineProperty(globalThis, key, { configurable: true, value });
await mkdir('node_modules/.cache/terminal-chat-error', { recursive: true });
await build({
  stdin: { contents: `export { useWebSocket } from './src/hooks/useWebSocket';
    export { useChatStore, ensureSessionRuntimes } from './src/stores';
    export { webClient } from './src/services/webClient';`, resolveDir: process.cwd() },
  bundle: true, packages: 'external', platform: 'node', format: 'esm',
  loader: { '.svg': 'empty', '.css': 'empty' },
  define: { 'import.meta.env': '{}' },
  outfile: 'node_modules/.cache/terminal-chat-error/fixture.mjs',
});
const { useWebSocket, useChatStore, ensureSessionRuntimes, webClient } =
  await import('../node_modules/.cache/terminal-chat-error/fixture.mjs');
const events = new Map();
webClient.on = (name, callback) => { events.set(name, callback); return () => events.delete(name); };
webClient.onStateChange = () => () => {};
webClient.connect = async () => {};
webClient.disconnect = async () => {};
webClient.request = async (_method, _params, options) => { options?.onRequestId?.('new-request'); return {}; };
webClient.getState = () => 'idle';
const root = createRoot(document.getElementById('root'));
let hook;
function Probe() { hook = useWebSocket({ activeSessionId: 'failed-session' }); return null; }
act(() => root.render(createElement(Probe)));

test('typed failed execution closes actual hook/store state without a processing-status frame', () => {
  ensureSessionRuntimes('failed-session');
  const chat = useChatStore.getState();
  chat.setProcessing('failed-session', true);
  chat.setThinking('failed-session', true);
  chat.appendReasoning('failed-session', 'Buffered reasoning');
  act(() => events.get('chat.error')({ payload: {
    session_id: 'failed-session', request_id: 'failed-request',
    error: 'OpenCode execution failed', code: 'model_output_limit_exceeded',
    terminal_status: 'failed',
  } }));
  const runtime = useChatStore.getState().getRuntime('failed-session');
  assert.equal(runtime.isProcessing, false);
  assert.equal(runtime.isThinking, false);
  assert.ok(runtime.messages.some(m => m.terminalStatus === 'failed' && m.errorCode === 'model_output_limit_exceeded'));
  assert.ok(runtime.reasoningSegments.every(s => s.status !== 'streaming'));
});

test('unknown outcome does not pretend execution has ended', () => {
  const chat = useChatStore.getState();
  chat.setProcessing('failed-session', true);
  act(() => events.get('chat.error')({ payload: {
    session_id: 'failed-session', request_id: 'unknown-request',
    error: 'Exit not confirmed', terminal_status: 'unknown',
  } }));
  assert.equal(useChatStore.getState().getRuntime('failed-session').isProcessing, true);
});

test('an older failed request cannot stop the next same-session turn', async () => {
  useChatStore.getState().setProcessing('failed-session', false);
  await act(async () => assert.equal(await hook.sendMessage('Continue', 'failed-session'), true));
  assert.equal(useChatStore.getState().getRuntime('failed-session').isProcessing, true);
  useChatStore.getState().setThinking('failed-session', true);
  act(() => events.get('chat.error')({ payload: {
    session_id: 'failed-session', request_id: 'late-old-request',
    error: 'Late error', terminal_status: 'failed',
  } }));
  assert.equal(useChatStore.getState().getRuntime('failed-session').isProcessing, true);
  assert.equal(useChatStore.getState().getRuntime('failed-session').isThinking, true);
  act(() => events.get('chat.error')({ payload: {
    session_id: 'failed-session', request_id: 'new-request',
    error: 'Current error', terminal_status: 'failed',
  } }));
  assert.equal(useChatStore.getState().getRuntime('failed-session').isProcessing, false);
});

test.after(() => { act(() => root.unmount()); dom.window.close(); });
