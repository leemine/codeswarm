import test from 'node:test';
import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';
const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  sessionStorage: dom.window.sessionStorage,
  CustomEvent: dom.window.CustomEvent,
  BroadcastChannel: undefined,
  IS_REACT_ACT_ENVIRONMENT: true,
});
dom.window.HTMLDialogElement.prototype.showModal = function () {
  this.open = true;
};
dom.window.HTMLDialogElement.prototype.close = function () {
  this.open = false;
};
await i18next
  .use(initReactI18next)
  .init({ lng: 'en', showSupportNotice: false, resources: { en: { translation: {} } } });
const base = '../node_modules/.cache/session-sharing/';
const { ShareSessionDialog } = await import(base + 'multi-session/dialogs/ShareSessionDialog.js');
const { sessionSharingApi } = await import(base + 'services/sessionSharingApi.js');
const { webClient } = await import(base + 'services/webClient.js');
const { notifyOrganizationCredentialChange } = await import(base + 'services/organizationCredentialEvents.js');
const original = { ...sessionSharingApi };
const originalRequest = webClient.request;
const find = (name) => document.querySelector(`[data-testid="multi-session-sharing-audit-${name}"]`);
const tick = () =>
  act(async () => {
    await new Promise((r) => setTimeout(r, 0));
  });
const event = (sequence = 1) => ({
  sequence,
  event_id: `event-${sequence}`,
  recorded_at: 1750000000,
  action: 'create',
  phase: 'mutation',
  result: 'committed',
  share_id: 'share-original',
  share_revision: 1,
  before_revision: 0,
  after_revision: 1,
  actor_id: 'alice',
  target_actor_id: 'bob',
  request_id: null,
  method: 'host_api',
});
const page = (session_id = 'session', events = [event()], has_more = false) => ({
  session_id,
  events,
  has_more,
  coverage: 'confirmed_mutations_and_publications_only',
});
let root;
const props = (extra = {}) => ({ sessionId: 'session', onClose: () => {}, ...extra });
async function mount(extra = {}) {
  root = createRoot(document.getElementById('root'));
  await act(async () => root.render(React.createElement(ShareSessionDialog, props(extra))));
  await tick();
}
async function click(name) {
  await act(async () => find(name).click());
  await tick();
}
test.beforeEach(() => {
  webClient.updateState('ready');
  sessionSharingApi.list = async () => ({ shares: [] });
  sessionSharingApi.audit = async (id) => page(id);
});
test.afterEach(async () => {
  if (root) await act(async () => root.unmount());
  root = null;
  Object.assign(sessionSharingApi, original);
  webClient.request = originalRequest;
});

test('query parser preserves exact owner selectors and rejects unsafe response fields', async () => {
  const calls = [];
  const controller = new AbortController();
  webClient.request = async (method, params, options) => {
    calls.push({ method, params, options });
    return page();
  };
  await original.audit('session', undefined, controller.signal);
  assert.deepEqual(calls[0].params, { session_id: 'session', limit: 50 });
  assert.equal(calls[0].method, 'session.share.audit.list');
  assert.equal(calls[0].options.signal, controller.signal);
  for (const limit of [0, 101, true, '50']) await assert.rejects(original.audit('session', limit));
  for (const invalid of [
    page('other'),
    { ...page(), coverage: 'all_activity' },
    page('session', [{ ...event(), seed_digest: 'private' }]),
    page('session', [event(), event()]),
    page('session', [{ ...event(), after_revision: 7 }]),
    page('session', [event()], true),
  ]) {
    webClient.request = async () => invalid;
    await assert.rejects(original.audit('session'));
  }
});

test('confirmed mutation and publication projections retain their distinct revision semantics', async () => {
  for (const action of ['create', 'update', 'revoke', 'continue']) {
    const row = { ...event(), action, method: `session.share.${action}` };
    if (action === 'continue')
      Object.assign(row, { phase: 'publication', before_revision: null, after_revision: null });
    webClient.request = async () => page('session', [row]);
    assert.equal((await original.audit('session')).events[0].action, action);
  }
});

for (const invalidation of ['credential', 'disconnect']) {
  test(`${invalidation} clears already displayed records without starting another query`, async () => {
    let calls = 0;
    sessionSharingApi.audit = async (id) => {
      calls++;
      return page(id);
    };
    await mount();
    await click('toggle');
    assert.ok(find('item'));
    await act(async () =>
      invalidation === 'credential' ? notifyOrganizationCredentialChange() : webClient.updateState('reconnecting'),
    );
    assert.equal(find('item'), null);
    assert.equal(find('toggle'), null);
    await act(async () => webClient.updateState('ready'));
    assert.equal(find('item'), null);
    assert.equal(calls, 1);
  });
}

test('owner section is opt-in, bounded, text-only and states incomplete coverage', async () => {
  const calls = [];
  sessionSharingApi.audit = async (id, limit) => {
    calls.push([id, limit]);
    return page(
      id,
      Array.from({ length: limit }, (_, n) => ({
        ...event(limit - n),
        actor_id: '<img src="/private">',
        target_actor_id: 'bob',
      })),
      true,
    );
  };
  await mount();
  assert.equal(calls.length, 0);
  await click('toggle');
  assert.deepEqual(calls, [['session', 50]]);
  assert.equal(document.querySelectorAll('[data-testid="multi-session-sharing-audit-item"]').length, 50);
  assert.equal(find('coverage').textContent, i18next.t('sessionSharing.audit.coverage'));
  assert.ok(find('more'));
  assert.equal(find('content').querySelectorAll('img,a,iframe').length, 0);
  await click('latest-hundred');
  assert.deepEqual(calls[1], ['session', 100]);
  assert.equal(find('latest-hundred'), null);
  assert.ok(find('more'));
  await click('toggle');
  assert.equal(find('content'), null);
});

test('inbox and non-owner source never expose or trigger owner audit', async () => {
  let calls = 0;
  sessionSharingApi.audit = async () => {
    calls++;
    return page();
  };
  sessionSharingApi.list = async (id) => {
    if (id) throw new Error('forbidden');
    return { shares: [] };
  };
  await mount();
  assert.equal(find('toggle'), null);
  await act(async () => root.render(React.createElement(ShareSessionDialog, props({ sessionId: undefined }))));
  await tick();
  assert.equal(find('toggle'), null);
  assert.equal(calls, 0);
});

for (const invalidation of ['collapse', 'close', 'session', 'credential', 'disconnect', 'unmount']) {
  test(`${invalidation} cancels owner query and late replies cannot refill content`, async () => {
    let complete,
      signal,
      calls = 0;
    sessionSharingApi.audit = (id, limit, s) => {
      calls++;
      signal = s;
      return new Promise((r) => {
        complete = r;
      });
    };
    await mount();
    await click('toggle');
    if (invalidation === 'collapse') await click('toggle');
    if (invalidation === 'close')
      await act(async () => document.querySelector('[data-testid="multi-session-sharing-close"]').click());
    if (invalidation === 'session')
      await act(async () => root.render(React.createElement(ShareSessionDialog, props({ sessionId: 'other' }))));
    if (invalidation === 'credential') await act(async () => notifyOrganizationCredentialChange());
    if (invalidation === 'disconnect') await act(async () => webClient.updateState('reconnecting'));
    if (invalidation === 'unmount') {
      await act(async () => root.unmount());
      root = null;
    }
    assert.equal(signal.aborted, true);
    await act(async () => complete(page()));
    await tick();
    assert.equal(find('item'), null);
    assert.equal(calls, 1);
  });
}

test('credential invalidation also prevents late owner-inventory from enabling query', async () => {
  let complete, signal;
  sessionSharingApi.list = (id, s) => {
    if (!id) return Promise.resolve({ shares: [] });
    signal = s;
    return new Promise((r) => {
      complete = r;
    });
  };
  await mount();
  await act(async () => notifyOrganizationCredentialChange());
  assert.equal(signal.aborted, true);
  await act(async () => complete({ shares: [] }));
  await tick();
  assert.equal(find('toggle'), null);
});

test('failed refresh clears cached events and never renders server error content', async () => {
  await mount();
  await click('toggle');
  assert.ok(find('item'));
  sessionSharingApi.audit = async () => {
    throw new Error('SECRET-SERVER-PAYLOAD');
  };
  await click('refresh');
  assert.equal(find('item'), null);
  assert.equal(find('error').textContent, i18next.t('sessionSharing.audit.error'));
  assert.equal(document.body.textContent.includes('SECRET-SERVER-PAYLOAD'), false);
});

test('identity invalidation during mutation unlocks closing and ignores its late result', async () => {
  const share = {
    share_id: 'grant',
    session_id: 'session',
    revision: 3,
    state: 'active',
    target_actor: 'bob',
    grantor_actor: 'alice',
    actions: ['view'],
    expires_at: Date.now() / 1000 + 3600,
    history_scope: 'fixed_snapshot',
    can_update: true,
    can_revoke: true,
  };
  sessionSharingApi.list = async () => ({ shares: [share] });
  let complete;
  sessionSharingApi.revoke = () =>
    new Promise((resolve) => {
      complete = resolve;
    });
  await mount();
  await click('toggle');
  const close = document.querySelector('[data-testid="multi-session-sharing-close"]');
  await act(async () => document.querySelector('[data-testid="multi-session-sharing-revoke"]').click());
  assert.equal(close.disabled, true);
  await act(async () => notifyOrganizationCredentialChange());
  assert.equal(close.disabled, false);
  assert.equal(find('item'), null);
  await act(async () => complete({ share_id: 'grant', revision: 4, audit: { degraded: true } }));
  assert.equal(document.querySelector('[data-testid="multi-session-sharing-audit-degraded"]'), null);
  assert.equal(find('toggle'), null);
});
