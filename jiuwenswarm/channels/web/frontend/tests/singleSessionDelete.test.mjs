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
const { DeletionAuditPendingError, archivedTaskClient, createArchivedTaskClient } = await import(
  base + 'features/workspace/archivedTaskClient.js'
);
const { useWorkspaceStore } = await import(base + 'stores/workspaceStore.js');
const { projectRegistryClient } = await import(base + 'features/workspace/projectRegistryClient.js');
const { useSessionStore } = await import(base + 'stores/sessionStore.js');
const actualLoadProjectSessions = useWorkspaceStore.getState().loadProjectSessions;
const { useCronStore } = await import(base + 'stores/cronStore.js');
const { toast } = await import(base + 'components/ui/Toast/toastStore.js');
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
  useCronStore.setState({ jobs: [], cronSessions: {}, expandedCronGroups: {}, loadJobs: async () => {} });
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

test('deleted audit-pending Session enters draft while original dialog stays mounted and retries same sid', async () => {
  const calls = [];
  await mount(async (method, params) => {
    calls.push({ method, params });
    return { session_id: 'a', deleted: true, exit_confirmed: true, audit_pending: calls.length === 1 };
  });
  await open('a');
  await click(q('multi-session-dialog-confirm'));
  assert.equal(row('a'), null);
  assert.equal(navigations.length, 1);
  // App preserves the chat Sidebar across current Session -> new draft.
  current.current = null;
  await act(async () => root.render(React.createElement(renderedProps.Harness)));
  assert.ok(q('multi-session-dialog'));
  assert.ok(q('multi-session-dialog-notice'));
  assert.equal(q('multi-session-dialog-confirm').disabled, false);
  await click(q('multi-session-dialog-confirm'));
  assert.deepEqual(calls.map(x => x.params), [{ session_id: 'a' }, { session_id: 'a' }]);
  assert.equal(q('multi-session-dialog'), null);
  assert.equal(navigations.length, 1);
});

test('late audit-pending A cannot attach warning or retry to B dialog', async () => {
  let resolve;
  await mount(async () => new Promise(r => { resolve = r; }));
  await open('a');
  await click(q('multi-session-dialog-confirm'));
  current.current = 'b';
  await act(async () => root.render(React.createElement(renderedProps.Harness)));
  await open('b');
  await act(async () => resolve({ session_id: 'a', deleted: true, exit_confirmed: true, audit_pending: true }));
  await tick();
  assert.equal(q('multi-session-dialog-notice'), null);
  assert.match(q('multi-session-dialog-description').textContent, /Session b/);
  assert.deepEqual(navigations, []);
});

for (const payload of [
  { session_id: 'a', audit_pending: true },
  { session_id: 'a', deleted: true, exit_confirmed: true, audit_pending: 'unknown' },
]) {
  test(`audit status cannot promote an unconfirmed receipt ${JSON.stringify(payload)}`, async () => {
    await assert.rejects(createArchivedTaskClient(async () => payload).deleteSession('a'),
      error => error.code === 'DELETE_UNCONFIRMED');
  });
}

async function deleteMenu(id) {
  await click(row(id).querySelector('[data-testid="multi-session-conversation-list-item-more"]'));
  await click([...document.querySelectorAll('[data-testid="multi-session-conversation-menu-item"][data-variant="delete"]')].at(-1));
}
for (const nested of [false, true]) {
  for (const exact of [true, false]) {
    test(`existing cron deletion consumer reconciles only its exact pending receipt nested=${nested} exact=${exact}`, async () => {
      const sid = 'cron_owned';
      let refreshes = 0;
      const notices = [];
      await mount(async () => ({ session_id: sid, deleted: true, exit_confirmed: true, audit_pending: true }));
      if (!exact) archivedTaskClient.deleteSession = async () => { throw new DeletionAuditPendingError('other'); };
      toast.open = event => { notices.push(event); return 'test'; };
      await act(async () => {
        if (nested) {
          useCronStore.setState({
            jobs: [{ id: 'job', name: 'Existing job', project_id: 'p', enabled: true }],
            expandedCronGroups: { 'cron-job': true },
            cronSessions: { job: [session(sid)] },
            loadCronSessions: async (projectId, jobId) => {
              assert.equal(projectId, 'p'); assert.equal(jobId, 'job');
              refreshes++;
              useCronStore.setState({ cronSessions: { job: [] } });
            },
          });
        } else {
          useWorkspaceStore.setState({
            pinnedSessions: [{ ...session(sid), pinned: true }],
            refreshWorkspaceData: async () => { refreshes++; },
          });
        }
      });
      await deleteMenu(sid);
      assert.equal(refreshes, exact ? 1 : 0);
      assert.equal(row(sid) === null, exact);
      assert.equal(notices.length, 1);
      assert.equal(q('multi-session-dialog'), null);
      assert.deepEqual(navigations, []);
    });
  }
}

const cleanup = (id) => ({ ...session(id), cleanup_only: true, title: '', project_dir: '', pinned: false });
async function restrictA() {
  projectRegistryClient.getSessions = async () => ({ sessions: [cleanup('a'), session('b')], total: 2 });
  await act(async () => actualLoadProjectSessions('p'));
}

test('refresh replaces cached private title with a cleanup-only row and its existing strict delete menu', async () => {
  const calls = [], selections = [];
  await mount(async (method, params) => {
    calls.push({ method, params });
    return { session_id: params.session_id, deleted: true, exit_confirmed: true };
  });
  useSessionStore.setState({ sessions: [{ ...session('a'), display_title: 'PRIVATE cached title' }] });
  await act(async () => root.render(React.createElement(renderedProps.Harness, {
    onSelect: value => selections.push(value),
  })));
  await restrictA();
  const target = row('a');
  assert.ok(target);
  assert.doesNotMatch(target.textContent, /PRIVATE|Session a/);
  assert.equal(target.querySelector('[data-testid="multi-session-conversation-list-item-main"]').disabled, true);
  await click(target.querySelector('[data-testid="multi-session-conversation-list-item-main"]'));
  assert.deepEqual(selections, []);
  assert.equal(target.querySelector('[data-testid="multi-session-conversation-list-item-pin"]'), null);
  await click(target.querySelector('[data-testid="multi-session-conversation-list-item-more"]'));
  assert.deepEqual([...document.querySelectorAll('[data-testid="multi-session-conversation-menu-item"]')]
    .map(item => item.dataset.variant), ['delete']);
  await click(document.querySelector('[data-testid="multi-session-conversation-menu-item"][data-variant="delete"]'));
  assert.doesNotMatch(q('multi-session-dialog-description').textContent, /PRIVATE|Session a/);
  await click(q('multi-session-dialog-confirm'));
  assert.deepEqual(calls, [{ method: 'session.delete', params: { session_id: 'a' } }]);
  assert.equal(row('a'), null);
  assert.ok(row('b'));
});

test('cleanup inventory clears pinned title and stale patch/upsert cannot restore content or navigation', async () => {
  await mount(async () => { throw Error('not called'); });
  await act(async () => useWorkspaceStore.setState({ pinnedSessions: [{ ...session('a'), pinned: true }] }));
  await restrictA();
  assert.equal(useWorkspaceStore.getState().pinnedSessions.length, 0);
  await act(async () => {
    useWorkspaceStore.getState().patchSession('a', { title: 'PRIVATE patch', display_title: 'PRIVATE preview' });
    useWorkspaceStore.getState().upsertSession({ ...session('a'), title: 'PRIVATE late upsert', model: 'oldmodel' });
  });
  const stored = useWorkspaceStore.getState().projectSessions.p.find(s => s.session_id === 'a');
  assert.equal(stored.cleanup_only, true);
  assert.equal(stored.title, '');
  assert.equal(stored.display_title, undefined);
  assert.equal(stored.model, undefined);
  assert.doesNotMatch(row('a').textContent, /PRIVATE|Session a/);
  // Only a new authorized inventory response restores a normal row.
  projectRegistryClient.getSessions = async () => ({ sessions: [session('a')], total: 1 });
  await act(async () => actualLoadProjectSessions('p'));
  assert.equal(row('a').querySelector('button').disabled, false);
  assert.match(row('a').textContent, /Session a/);
});

test('already open delete dialog drops its private title after cleanup-only refresh', async () => {
  await mount(async () => ({ session_id: 'a', deleted: true, exit_confirmed: true }));
  await open('a');
  await restrictA();
  assert.doesNotMatch(q('multi-session-dialog-description').textContent, /Session a/);
  await click(q('multi-session-dialog-confirm'));
  assert.deepEqual(deleted, ['a']);
});

test('cleanup-only late receipt cannot close a newer dialog', async () => {
  let resolve;
  await mount(async () => new Promise(r => { resolve = r; }));
  await restrictA();
  await deleteMenu('a');
  await click(q('multi-session-dialog-confirm'));
  current.current = 'b';
  await act(async () => root.render(React.createElement(renderedProps.Harness)));
  await open('b');
  await act(async () => resolve({ session_id: 'a', deleted: true, exit_confirmed: true }));
  assert.match(q('multi-session-dialog-description').textContent, /Session b/);
  assert.deepEqual(navigations, []);
  assert.ok(row('b'));
});

test('cleanup row cannot make legacy or unsupported mode deletion visible', async () => {
  await mount(async () => { throw Error('not called'); }, { organization: false });
  await restrictA();
  await click(row('a').querySelector('[data-testid="multi-session-conversation-list-item-more"]'));
  assert.equal(document.querySelector('[data-testid="multi-session-conversation-menu-item"]'), null);
});

for (const payload of [{ session_id: 'a', deleted: true }, { session_id: 'a', deleted: true, exit_confirmed: false }]) {
  test(`cleanup-only row remains retryable without confirmed exit ${JSON.stringify(payload)}`, async () => {
    await mount(async () => payload);
    await restrictA();
    await deleteMenu('a');
    await click(q('multi-session-dialog-confirm'));
    assert.ok(q('multi-session-dialog-error'));
    assert.equal(q('multi-session-dialog-confirm').disabled, false);
    assert.ok(row('a'));
    assert.deepEqual(deleted, []);
    assert.deepEqual(navigations, []);
  });
}
