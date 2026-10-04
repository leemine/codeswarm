import test from 'node:test';
import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';
const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  localStorage: dom.window.localStorage,
  sessionStorage: dom.window.sessionStorage,
  CustomEvent: dom.window.CustomEvent,
  IS_REACT_ACT_ENVIRONMENT: true,
});
dom.window.HTMLDialogElement.prototype.showModal = function () {
  this.open = true;
};
dom.window.HTMLDialogElement.prototype.close = function () {
  this.open = false;
};
const base = '../node_modules/.cache/session-delete/';
const { createArchivedTaskClient, archivedTaskClient } = await import(
  base + 'features/workspace/archivedTaskClient.js'
);
const { ArchivedTasksSettingsModule } = await import(
  base + 'features/settings/modules/archivedTasks/ArchivedTasksSettings.js'
);
const { SettingsServicesProvider } = await import(base + 'features/settings/services/SettingsServicesProvider.js');
const { useWorkspaceStore } = await import(base + 'stores/workspaceStore.js');
const { toast } = await import(base + 'components/ui/Toast/toastStore.js');
await i18next.changeLanguage('en');
for (const payload of [
  null,
  {},
  [],
  { session_id: 'other' },
  { session_id: 's', ok: false },
  { session_id: 's', deleted: false },
  { session_id: 's', success: false },
  { session_id: 's', stop_pending: true },
  { session_id: 's', recovery_required: true },
  { session_id: 's', exit_confirmed: false },
  { session_id: 's', error: 'failed' },
]) {
  test(`delete rejects unconfirmed success envelope: ${JSON.stringify(payload)}`, async () => {
    const client = createArchivedTaskClient(async () => payload);
    await assert.rejects(client.deleteSession('s'), { code: 'DELETE_UNCONFIRMED' });
  });
}
for (const payload of [{ session_id: 's' }, { session_id: 's', project_id: 'p' }]) {
  test(`legacy exact deletion remains accepted: ${JSON.stringify(payload)}`, async () => {
    let sent;
    const client = createArchivedTaskClient(async (...args) => {
      sent = args;
      return payload;
    });
    assert.deepEqual(await client.deleteSession('s'), payload);
    assert.deepEqual(sent, ['session.delete', { session_id: 's' }]);
  });
}
const row = (id) => ({
  session_id: id,
  title: `Title ${id}`,
  project_id: 'p',
  project_name: 'Project',
  work_mode: 'work',
  archived: true,
  archived_at: 1,
  stop_pending: false,
});
const tick = () =>
  act(async () => {
    await new Promise((r) => setTimeout(r, 0));
  });
const click = async (element) => {
  assert.ok(element);
  await act(async () => element.click());
  await tick();
};
const deleteButton = (id) =>
  document.querySelector(`[data-testid="archived-tasks-session-delete"][data-variant="${id}"]`);
const confirm = () => document.querySelector('dialog[open] .settings-confirm-dialog__footer button:last-child');
let root, toasts, listCalls, refreshes;
async function mount(del) {
  toasts = [];
  listCalls = 0;
  refreshes = 0;
  toast.open = (event) => {
    toasts.push(event);
    return 'test';
  };
  archivedTaskClient.listArchivedSessions = async () => {
    listCalls++;
    return { sessions: [row('a'), row('b')], total: 2, limit: 20, offset: 0, has_more: false };
  };
  archivedTaskClient.deleteSession = createArchivedTaskClient(del).deleteSession;
  useWorkspaceStore.setState({
    workMode: 'work',
    refreshWorkspaceData: async () => {
      refreshes++;
    },
  });
  root = createRoot(document.getElementById('root'));
  await act(async () =>
    root.render(
      React.createElement(
        SettingsServicesProvider,
        { isConnected: true, connectionState: 'ready', request: async () => ({}) },
        React.createElement(ArchivedTasksSettingsModule),
      ),
    ),
  );
  await tick();
}
async function unmount() {
  if (root) {
    await act(async () => root.unmount());
    root = null;
  }
}
test.afterEach(unmount);
for (const code of ['NOT_FOUND', 'DELETE_FAILED', 'STOP_TIMEOUT']) {
  test(`actual delete dialog keeps ${code} retryable and never announces success`, async () => {
    await mount(async () => {
      throw Object.assign(new Error('failure'), { code });
    });
    await click(deleteButton('a'));
    await click(confirm());
    assert.ok(document.querySelector('dialog[open] [role="alert"]'));
    if (code === 'NOT_FOUND') assert.match(document.querySelector('dialog[open]').textContent, /state is unknown/);
    assert.equal(confirm().disabled, false);
    assert.equal(toasts.length, 0);
    assert.ok(deleteButton('a'));
    assert.ok(listCalls >= 2);
    assert.equal(refreshes, 1);
  });
}
test('actual dialog rejects wrong-session RPC success', async () => {
  await mount(async () => ({ session_id: 'b' }));
  await click(deleteButton('a'));
  await click(confirm());
  assert.match(document.querySelector('dialog[open]').textContent, /state is unknown/);
  assert.equal(toasts.length, 0);
  assert.equal(confirm().disabled, false);
});
test('actual legacy success closes matching dialog and announces success', async () => {
  await mount(async () => ({ session_id: 'a' }));
  await click(deleteButton('a'));
  await click(confirm());
  assert.equal(document.querySelector('dialog[open]'), null);
  assert.equal(toasts.length, 1);
  assert.equal(toasts[0].variant, 'success');
});
test('late original-target success cannot close a new target dialog or clear its pending state', async () => {
  const resolvers = new Map();
  await mount(async (_method, params) => new Promise((resolve) => resolvers.set(params.session_id, resolve)));
  await click(deleteButton('a'));
  await click(confirm());
  assert.equal(deleteButton('a').disabled, true);
  assert.equal(deleteButton('b').disabled, false);
  // Direct DOM event exercises the target-change race even though the real modal traps pointer input.
  await click(deleteButton('b'));
  await click(confirm());
  await act(async () => resolvers.get('a')({ session_id: 'a' }));
  await tick();
  assert.ok(document.querySelector('dialog[open]'));
  assert.match(document.querySelector('dialog[open]').textContent, /Title b/);
  assert.equal(confirm().disabled, true);
  assert.equal(toasts.length, 0);
  await act(async () => resolvers.get('b')({ session_id: 'b' }));
  await tick();
  assert.equal(document.querySelector('dialog[open]'), null);
  assert.equal(toasts.length, 1);
});
test('unmounted dialog ignores its late successful completion', async () => {
  let resolve;
  await mount(async () => new Promise((r) => (resolve = r)));
  await click(deleteButton('a'));
  await click(confirm());
  await unmount();
  await act(async () => resolve({ session_id: 'a' }));
  await tick();
  assert.equal(toasts.length, 0);
  assert.equal(refreshes, 0);
});

const { useSideConversationDeletion } = await import(base + 'multi-session/state/useSideConversationDeletion.js');
let sideControls;
function SideHarness({ removeLocal }) {
  const [side, setSide] = React.useState({ session: { session_id: 'a' } });
  const sideRef = React.useRef(side);
  const deleteSide = useSideConversationDeletion(sideRef, setSide, removeLocal);
  sideControls = {
    deleteSide,
    getCurrent: () => sideRef.current,
    open: (id) => {
      const value = { session: { session_id: id } };
      sideRef.current = value;
      setSide(value);
    },
  };
  return React.createElement('div', { 'data-testid': 'side-pane' }, side?.session.session_id ?? 'closed');
}
async function mountSide(request) {
  archivedTaskClient.deleteSession = createArchivedTaskClient(request).deleteSession;
  const removed = [];
  root = createRoot(document.getElementById('root'));
  await act(async () => root.render(React.createElement(SideHarness, { removeLocal: (id) => removed.push(id) })));
  return removed;
}
test('actual side-deletion hook only removes original Session; old completion keeps new pane and ref', async () => {
  let resolve;
  let calls = 0;
  const removed = await mountSide(async () => {
    calls++;
    return new Promise((r) => (resolve = r));
  });
  let deletion, duplicate;
  await act(async () => {
    deletion = sideControls.deleteSide('a');
    duplicate = sideControls.deleteSide('a');
  });
  assert.equal(deletion, duplicate);
  assert.equal(calls, 1);
  await act(async () => sideControls.open('b'));
  await act(async () => {
    resolve({ session_id: 'a' });
    await deletion;
  });
  assert.deepEqual(removed, ['a']);
  assert.equal(sideControls.getCurrent().session.session_id, 'b');
  assert.equal(document.querySelector('[data-testid="side-pane"]').textContent, 'b');
});
test('actual side deletion rejects malformed receipt, keeps pane, and permits a confirmed retry', async () => {
  let valid = false;
  const removed = await mountSide(async () => (valid ? { session_id: 'a' } : { session_id: 'b' }));
  await act(async () => assert.rejects(sideControls.deleteSide('a'), { code: 'DELETE_UNCONFIRMED' }));
  assert.deepEqual(removed, []);
  assert.equal(sideControls.getCurrent().session.session_id, 'a');
  valid = true;
  await act(async () => sideControls.deleteSide('a'));
  assert.deepEqual(removed, ['a']);
  assert.equal(sideControls.getCurrent(), null);
  assert.equal(document.querySelector('[data-testid="side-pane"]').textContent, 'closed');
});

const { SessionsPanel } = await import(base + 'components/SessionsPanel/index.js');
const { webClient } = await import(base + 'services/webClient.js');
const panelButton = (id) =>
  document.querySelector(
    `[data-testid="sessions-panel-session-row"][data-variant="${id}"] [data-testid="sessions-panel-session-item-delete"]`,
  );
async function mountSessionsPanel(request) {
  window.confirm = () => true;
  globalThis.fetch = async (url) => ({
    ok: String(url).startsWith('/file-api/list-files'),
    json: async () => ({ files: [] }),
  });
  webClient.request = async (method, params) =>
    method === 'session.list'
      ? {
          sessions: [
            { session_id: 'a', title: 'A' },
            { session_id: 'b', title: 'B' },
          ],
        }
      : request(method, params);
  archivedTaskClient.deleteSession = createArchivedTaskClient((...args) => webClient.request(...args)).deleteSession;
  root = createRoot(document.getElementById('root'));
  await act(async () =>
    root.render(
      React.createElement(SessionsPanel, {
        currentSessionId: 'a',
        isConnected: true,
        isProcessing: false,
        onRestoreSession() {},
      }),
    ),
  );
  await tick();
}
test('actual SessionsPanel rejects false deletion receipt and keeps retry available', async () => {
  await mountSessionsPanel(async () => ({ session_id: 'b' }));
  await click(panelButton('a'));
  assert.ok(
    document.querySelector('[data-testid="sessions-panel-error"]') ||
      document.body.textContent.includes('Failed to delete'),
  );
  assert.equal(panelButton('a').disabled, false);
});
test('actual SessionsPanel concurrent deletions retain the other target pending state', async () => {
  const resolvers = new Map();
  await mountSessionsPanel(
    async (_method, params) => new Promise((resolve) => resolvers.set(params.session_id, resolve)),
  );
  await click(panelButton('a'));
  await click(panelButton('b'));
  assert.equal(panelButton('a').disabled, true);
  assert.equal(panelButton('b').disabled, true);
  await act(async () => resolvers.get('a')({ session_id: 'a' }));
  await tick();
  assert.equal(panelButton('a').disabled, false);
  assert.equal(panelButton('b').disabled, true);
  await act(async () => resolvers.get('b')({ session_id: 'b' }));
  await tick();
  assert.equal(panelButton('b').disabled, false);
});

test('archived deletion pending retains existing dialog with audit-only retry and no success toast', async () => {
  const calls = [];
  await mount(async (_method, params) => {
    calls.push(params.session_id);
    return { session_id: 'a', deleted: true, exit_confirmed: true, audit_pending: calls.length === 1 };
  });
  await click(deleteButton('a'));
  await click(confirm());
  assert.match(document.querySelector('dialog[open]').textContent, /audit.*pending/i);
  assert.match(confirm().textContent, /retry saving audit/i);
  assert.equal(toasts.length, 0);
  await click(confirm());
  assert.deepEqual(calls, ['a', 'a']);
  assert.equal(document.querySelector('dialog[open]'), null);
});

test('side deletion pending removes exact deleted pane but reports pending to its existing feedback caller', async () => {
  const removed = await mountSide(async () => ({ session_id: 'a', deleted: true, exit_confirmed: true, audit_pending: true }));
  await act(async () => assert.rejects(sideControls.deleteSide('a'), { code: 'DELETE_AUDIT_PENDING' }));
  assert.deepEqual(removed, ['a']);
  assert.equal(sideControls.getCurrent(), null);
});

test('SessionsPanel pending deletion shows fixed warning instead of a failure or ordinary success', async () => {
  await mountSessionsPanel(async () => ({ session_id: 'a', deleted: true, exit_confirmed: true, audit_pending: true }));
  await click(panelButton('a'));
  assert.match(document.body.textContent, /audit.*pending/i);
});
