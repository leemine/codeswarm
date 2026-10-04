import test from 'node:test';
import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  localStorage: dom.window.localStorage,
  sessionStorage: dom.window.sessionStorage,
  CustomEvent: dom.window.CustomEvent,
  HTMLElement: dom.window.HTMLElement,
  Node: dom.window.Node,
  MutationObserver: dom.window.MutationObserver,
  IS_REACT_ACT_ENVIRONMENT: true,
});
const base = '../node_modules/.cache/single-session-delete/';
const { ConversationSidebar } = await import(base + 'multi-session/sidebar/ConversationSidebar.js');
const { archivedTaskClient, createArchivedTaskClient } = await import(
  base + 'features/workspace/archivedTaskClient.js'
);
const { useWorkspaceStore } = await import(base + 'stores/workspaceStore.js');
const { useCronStore } = await import(base + 'stores/cronStore.js');
const { useSessionDeletionReceipt } = await import(base + 'multi-session/state/useSideConversationDeletion.js');
const q = (id) => document.querySelector(`[data-testid="${id}"]`);
const tick = () =>
  act(async () => {
    await new Promise((r) => setTimeout(r, 0));
  });
const click = async (element) => {
  assert.ok(element);
  await act(async () => element.click());
  await tick();
};
const row = (id) =>
  document.querySelector(`[data-testid="multi-session-conversation-list-item"][data-variant="${id}"]`);
const session = (id, mode = 'agent') => ({
  session_id: id,
  title: `Session ${id}`,
  mode,
  project_id: 'p',
  created_at: 1,
  updated_at: 1,
});
let root, deleted, navigations, current, renderedProps;
async function mount(response, { organization = true, mode = 'agent' } = {}) {
  deleted = [];
  navigations = [];
  current = { current: 'a' };
  archivedTaskClient.deleteSession = createArchivedTaskClient(response).deleteSession;
  useCronStore.setState({ jobs: [], loadJobs: async () => {} });
  useWorkspaceStore.setState({
    workMode: 'work',
    projects: [{ project_id: 'p', name: 'Project', work_mode: 'work', session_count: 2 }],
    projectSessions: { p: [session('a', mode), session('b')] },
    projectSessionTotals: { p: 2 },
    sessionVisibility: { p: { visibleCount: 10 } },
    pinnedSessions: [],
    expandedProjectIds: { p: true },
    loadProjectSessions: async () => {},
    refreshWorkspaceData: async () => {},
  });
  function Harness(props) {
    const receipt = useSessionDeletionReceipt(
      current,
      'agent',
      (mode, options, lifecycle) => navigations.push({ mode, options, lifecycle }),
      (sid) => deleted.push(sid),
    );
    return React.createElement(ConversationSidebar, {
      activeSessionId: current.current,
      onNew: () => {},
      onSelect: () => {},
      onOpenCron: () => {},
      isCronActive: false,
      onOpenSharedSessions: organization ? () => {} : undefined,
      onSessionDeleted: receipt,
      ...props,
    });
  }
  renderedProps = { Harness };
  root = createRoot(document.getElementById('root'));
  await act(async () => root.render(React.createElement(Harness)));
  await tick();
}
async function open(id) {
  await click(row(id).querySelector('[data-testid="multi-session-conversation-list-item-more"]'));
  await click(
    [...document.querySelectorAll('[data-testid="multi-session-conversation-menu-item"][data-variant="delete"]')].at(
      -1,
    ),
  );
  assert.match(q('multi-session-dialog-description').textContent, new RegExp(`Session ${id}`));
}
test.afterEach(async () => {
  if (root) {
    await act(async () => root.unmount());
    root = null;
  }
});
for (const payload of [
  { session_id: 'a' },
  { session_id: 'a', deleted: true },
  { session_id: 'a', exit_confirmed: true },
  { session_id: 'a', deleted: false, exit_confirmed: true },
  { session_id: 'a', deleted: true, exit_confirmed: false },
  { session_id: 'b', deleted: true, exit_confirmed: true },
]) {
  test(`ordinary Single retains retry on unconfirmed receipt ${JSON.stringify(payload)}`, async () => {
    await mount(async () => payload);
    await open('a');
    await click(q('multi-session-dialog-confirm'));
    assert.ok(q('multi-session-dialog-error'));
    assert.equal(q('multi-session-dialog-confirm').disabled, false);
    assert.ok(row('a'));
    assert.deepEqual(deleted, []);
    assert.deepEqual(navigations, []);
  });
}
test('confirmed current Single removes only itself and enters a draft with no retired previous Session', async () => {
  let sent;
  await mount(async (...args) => {
    sent = args;
    return { session_id: 'a', deleted: true, exit_confirmed: true };
  });
  await open('a');
  await click(q('multi-session-dialog-confirm'));
  assert.deepEqual(sent, ['session.delete', { session_id: 'a' }]);
  assert.equal(row('a'), null);
  assert.ok(row('b'));
  assert.equal(q('multi-session-dialog'), null);
  assert.deepEqual(deleted, ['a']);
  assert.deepEqual(navigations, [{ mode: 'agent', options: {}, lifecycle: { clearPreviousSession: true } }]);
});
test('late A acknowledgement cannot clear the B dialog or navigate the now-current B Session', async () => {
  const resolve = new Map();
  await mount(async (_m, p) => new Promise((r) => resolve.set(p.session_id, r)));
  await open('a');
  await click(q('multi-session-dialog-confirm'));
  current.current = 'b';
  await act(async () => root.render(React.createElement(renderedProps.Harness)));
  await open('b');
  await click(q('multi-session-dialog-confirm'));
  await act(async () => resolve.get('a')({ session_id: 'a', deleted: true, exit_confirmed: true }));
  await tick();
  assert.ok(q('multi-session-dialog'));
  assert.match(q('multi-session-dialog-description').textContent, /Session b/);
  assert.equal(q('multi-session-dialog-confirm').disabled, true);
  assert.ok(row('b'));
  assert.deepEqual(deleted, ['a']);
  assert.deepEqual(navigations, []);
  await act(async () => resolve.get('b')({ session_id: 'b', deleted: true, exit_confirmed: true }));
});
for (const options of [{ organization: false }, { mode: 'team' }]) {
  test(`new delete action is not offered outside organization Single ${JSON.stringify(options)}`, async () => {
    await mount(async () => {
      throw Error('must not request');
    }, options);
    await click(row('a').querySelector('[data-testid="multi-session-conversation-list-item-more"]'));
    assert.equal(
      document.querySelector('[data-testid="multi-session-conversation-menu-item"][data-variant="delete"]'),
      null,
    );
  });
}
test('legacy callers still accept their original exact Session receipt', async () => {
  const client = createArchivedTaskClient(async () => ({ session_id: 'old' }));
  assert.deepEqual(await client.deleteSession('old'), { session_id: 'old' });
  await assert.rejects(client.deleteSession('old', { requireExitConfirmation: true }), { code: 'DELETE_UNCONFIRMED' });
});

test('NOT_FOUND refreshes inventory but retains an unknown, retryable deletion', async () => {
  let refreshes = 0;
  await mount(async () => {
    throw Object.assign(new Error('missing'), { code: 'NOT_FOUND' });
  });
  useWorkspaceStore.setState({
    refreshWorkspaceData: async () => {
      refreshes++;
    },
  });
  await open('a');
  await click(q('multi-session-dialog-confirm'));
  assert.ok(q('multi-session-dialog-error'));
  assert.equal(q('multi-session-dialog-confirm').disabled, false);
  assert.equal(refreshes, 1);
  assert.deepEqual(deleted, []);
  assert.deepEqual(navigations, []);
});
