import assert from 'node:assert/strict';
import test, { after } from 'node:test';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { I18nextProvider } from 'react-i18next';
import { JSDOM } from 'jsdom';

// Reserved DOM origin only. HTTP is rejected; WebSocket is an in-memory transport, never a network client.
const dom = new JSDOM('<div id="root"></div>', { url: 'https://permission-answer.invalid', pretendToBeVisual: true });
class MemoryWebSocket {
  static OPEN = 1;
  static instance;
  constructor(url) {
    assert.equal(new URL(url).hostname, 'permission-answer.invalid');
    this.readyState = 0;
    this.requests = [];
    this.listeners = new Map();
    MemoryWebSocket.instance = this;
    queueMicrotask(() => { this.readyState = 1; this.onopen?.(); });
  }
  addEventListener(name, handler) {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), handler]);
  }
  send(raw) { this.requests.push(JSON.parse(raw)); }
  receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
  close(code = 1000, reason = '') {
    this.readyState = 3;
    const event = { code, reason, wasClean: true };
    this.onclose?.(event);
    for (const handler of this.listeners.get('close') ?? []) handler(event);
  }
}
for (const [key, value] of Object.entries({
  window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
  localStorage: dom.window.localStorage, HTMLElement: dom.window.HTMLElement, Node: dom.window.Node,
  Event: dom.window.Event, CustomEvent: dom.window.CustomEvent, IS_REACT_ACT_ENVIRONMENT: true,
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  fetch: () => { throw new Error('Unexpected HTTP request'); }, WebSocket: MemoryWebSocket,
})) Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
after(() => dom.window.close());

const { useWebSocket } = await import('../node_modules/.cache/input-area-permission-merge/hooks/useWebSocket.js');
const { useChatStore, useSessionStore } = await import('../node_modules/.cache/input-area-permission-merge/stores/index.js');
const { default: i18n } = await import('../node_modules/.cache/input-area-permission-merge/i18n/index.js');
const { webClient } = await import('../node_modules/.cache/input-area-permission-merge/services/webClient.js');

async function mounted(organizationAuth, run) {
  const sessionId = 'organization-release';
  useChatStore.getState().ensureRuntime(sessionId);
  useSessionStore.getState().ensureRuntime(sessionId);
  useSessionStore.getState().setMode(sessionId, 'agent');
  let api;
  function Host() { api = useWebSocket({ activeSessionId: sessionId, organizationAuth }); return null; }
  const root = createRoot(document.getElementById('root'));
  const render = async () => act(async () => root.render(createElement(I18nextProvider, { i18n }, createElement(Host))));
  try {
    await render();
    await run({ sessionId, get api() { return api; }, socket: MemoryWebSocket.instance,
      async setOrganization(value) { organizationAuth = value; await render(); } });
  } finally {
    await act(async () => root.unmount());
    await webClient.disconnect();
    useChatStore.getState().removeRuntime(sessionId);
    useSessionStore.getState().removeRuntime(sessionId);
  }
}
async function exchange(f, method, params) {
  const promise = f.api.request(method, params);
  await act(async () => {});
  const sent = f.socket.requests.at(-1);
  assert.equal(sent.method, method);
  await act(async () => f.socket.receive({ type: 'res', id: sent.id, ok: true, payload: { accepted: true } }));
  await promise;
}

test('actual organization hook refuses Goal/attach/Team before wire, preserving read-only and Single requests', async () => {
  await mounted(true, async (f) => {
    for (const action of ['set', 'pause', 'resume', 'clear']) {
      await assert.rejects(f.api.request('command.goal', { session_id: f.sessionId, action }));
    }
    await assert.rejects(f.api.request('chat.send', { session_id: f.sessionId, attach_goal: true }));
    await assert.rejects(f.api.request('session.create', { mode: 'team.work.normal' }));
    useSessionStore.getState().setMode(f.sessionId, 'team');
    await assert.rejects(f.api.request('chat.send', { session_id: f.sessionId, mode: 'agent' }));
    await assert.rejects(f.api.request('chat.user_answer', { session_id: f.sessionId, answer: 'yes' }));
    await act(async () => f.api.setGoalObjective(f.sessionId, 'not sent'));
    assert.equal(f.socket.requests.length, 0);
    useSessionStore.getState().setMode(f.sessionId, 'agent');
    await exchange(f, 'command.goal', { session_id: f.sessionId, action: 'get' });
    await exchange(f, 'chat.send', { session_id: f.sessionId, content: 'ordinary Single', mode: 'agent' });
    await exchange(f, 'chat.user_answer', { session_id: f.sessionId, answer: 'yes' });
    assert.equal(f.socket.requests.length, 3);
  });
});

test('legacy wire behavior remains, while retained callbacks use current trusted organization state', async () => {
  await mounted(false, async (f) => {
    await exchange(f, 'session.create', { mode: 'team' });
    await exchange(f, 'command.goal', { session_id: f.sessionId, action: 'set', objective: 'legacy' });
    const previousRequest = f.api.request;
    const previousSetGoal = f.api.setGoalObjective;
    const count = f.socket.requests.length;
    await f.setOrganization(true);
    await assert.rejects(previousRequest('command.goal', { session_id: f.sessionId, action: 'resume' }));
    await act(async () => previousSetGoal(f.sessionId, 'stale callback'));
    assert.equal(f.socket.requests.length, count);
  });
});
