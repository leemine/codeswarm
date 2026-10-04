import test from 'node:test';
import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';
const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
const channels = new Set();
class FakeBroadcastChannel {
  constructor(name) {
    this.name = name;
    channels.add(this);
  }
  postMessage(data) {
    for (const channel of channels) if (channel !== this && channel.name === this.name) channel.onmessage?.({ data });
  }
  close() {
    channels.delete(this);
  }
}
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  sessionStorage: dom.window.sessionStorage,
  CustomEvent: dom.window.CustomEvent,
  BroadcastChannel: FakeBroadcastChannel,
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
const { SharedHistoryDialog } =
  await import('../node_modules/.cache/session-sharing/multi-session/dialogs/SharedHistoryDialog.js');
const { sessionSharingApi } = await import('../node_modules/.cache/session-sharing/services/sessionSharingApi.js');
const { webClient } = await import('../node_modules/.cache/session-sharing/services/webClient.js');
const { notifyOrganizationCredentialChange } =
  await import('../node_modules/.cache/session-sharing/services/organizationCredentialEvents.js');
const originalHistory = sessionSharingApi.history;
const originalViewGrant = sessionSharingApi.viewGrant;
test.beforeEach(() => {
  sessionSharingApi.viewGrant = async () => ({ revision: 1, expires_at: null });
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
  webClient.updateState('ready');
});
test.afterEach(() => {
  sessionSharingApi.viewGrant = originalViewGrant;
});
const target = { session_id: 'alice-private', share_id: 'share-for-bob' };
const page = (content = 'private text', cursor = null, forTarget = target) => ({
  ...forTarget,
  read_only: true,
  messages: [{ role: 'user', content, id: content }],
  next_cursor: cursor,
});
const tick = () =>
  act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
const find = (name) => document.querySelector(`[data-testid="multi-session-shared-history-${name}"]`);
let root;
async function mount(props = {}) {
  root = createRoot(document.getElementById('root'));
  await act(async () => root.render(React.createElement(SharedHistoryDialog, { target, onClose: () => {}, ...props })));
  await tick();
}
async function unmount() {
  await act(async () => root.unmount());
  sessionSharingApi.history = originalHistory;
}

test('only bounded sharing history RPC; identity/authority extra fields cannot enter request', async () => {
  const original = webClient.request;
  const calls = [];
  webClient.request = async (method, params) => {
    calls.push({ method, params });
    return page();
  };
  try {
    await originalHistory({ ...target, user_id: 'alice', authority: 'forged', limit: 999 });
    await originalHistory(target, 'opaque');
    assert.deepEqual(calls, [
      { method: 'session.share.history.get', params: { ...target, limit: 50 } },
      { method: 'session.share.history.get', params: { ...target, cursor: 'opaque', limit: 50 } },
    ]);
    webClient.request = async () => ({ ...page(), session_id: 'other' });
    await assert.rejects(originalHistory(target));
    webClient.request = async () => ({ ...page(), read_only: false });
    await assert.rejects(originalHistory(target));
    webClient.request = async () => ({ ...page(), messages: [{ role: 'tool', content: 'private' }] });
    await assert.rejects(originalHistory(target));
  } finally {
    webClient.request = original;
  }
});

test('text never loads media, links, HTML or action controls', async () => {
  const content = '![media](https://secret/image) <img src="/file-api/private"> [tool](javascript:alert(1))';
  sessionSharingApi.history = async () => page(content);
  await mount();
  try {
    assert.equal(find('text').textContent, content);
    assert.equal(document.querySelectorAll('img,a,iframe,video,audio,input,textarea,form').length, 0);
    assert.equal(document.querySelectorAll('button').length, 2);
  } finally {
    await unmount();
  }
});

test('newest first pages append older; refresh discards cursor and all old pages', async () => {
  const calls = [];
  sessionSharingApi.history = async (t, cursor) => {
    calls.push([t, cursor]);
    return cursor ? page('older') : page('newest', 'cursor-1');
  };
  await mount();
  try {
    await act(async () => find('older').click());
    await tick();
    assert.deepEqual(
      [...document.querySelectorAll('[data-testid="multi-session-shared-history-text"]')].map((x) => x.textContent),
      ['newest', 'older'],
    );
    assert.equal(find('older'), null);
    await act(async () => find('refresh').click());
    await tick();
    assert.equal(document.querySelectorAll('[data-testid="multi-session-shared-history-message"]').length, 1);
    assert.deepEqual(
      calls.map((c) => c[1]),
      [undefined, 'cursor-1', undefined],
    );
  } finally {
    await unmount();
  }
});

test('revoked older page immediately clears all content and cursor without fallback', async () => {
  sessionSharingApi.history = async (t, cursor) => {
    if (cursor) throw Object.assign(new Error('revoked'), { code: 'FORBIDDEN' });
    return page('private', 'c');
  };
  await mount();
  try {
    await act(async () => find('older').click());
    await tick();
    assert.ok(find('error'));
    assert.equal(find('text'), null);
    assert.equal(find('older'), null);
  } finally {
    await unmount();
  }
});

test('refresh clears old text while rechecking and transport failure stays clear', async () => {
  sessionSharingApi.history = async () => page();
  await mount();
  let reject;
  try {
    sessionSharingApi.history = () =>
      new Promise((_, r) => {
        reject = r;
      });
    await act(async () => find('refresh').click());
    assert.equal(find('text'), null);
    await act(async () => reject(new Error('offline')));
    await tick();
    assert.ok(find('error'));
    assert.equal(find('older'), null);
  } finally {
    await unmount();
  }
});

test('switching share ignores old response and starts from fresh cursor', async () => {
  let resolveOld;
  const other = { session_id: 'another', share_id: 'other' };
  sessionSharingApi.history = (t) =>
    t.share_id === target.share_id
      ? new Promise((r) => {
          resolveOld = r;
        })
      : Promise.resolve(page('other', null, other));
  await mount();
  try {
    await act(async () => root.render(React.createElement(SharedHistoryDialog, { target: other, onClose: () => {} })));
    await tick();
    await act(async () => resolveOld(page('late old', 'old-cursor')));
    await tick();
    assert.equal(find('text').textContent, 'other');
    assert.equal(find('older'), null);
  } finally {
    await unmount();
  }
});

test('close clears content and invalidates delayed response even before host unmounts', async () => {
  let resolve;
  let closed = 0;
  sessionSharingApi.history = () =>
    new Promise((r) => {
      resolve = r;
    });
  await mount({
    onClose: () => {
      closed++;
    },
  });
  try {
    await act(async () => find('close').click());
    await act(async () => resolve(page('late')));
    await tick();
    assert.equal(closed, 1);
    assert.equal(find('text'), null);
    assert.equal(find('older'), null);
  } finally {
    await unmount();
  }
});

for (const signal of ['same-tab-credentials', 'other-tab-credentials', 'disconnect', 'pagehide', 'hidden']) {
  test(`${signal} clears content and invalidates pending reads`, async () => {
    sessionSharingApi.history = async () => page('private', 'c');
    await mount();
    let resolve;
    try {
      sessionSharingApi.history = () =>
        new Promise((r) => {
          resolve = r;
        });
      await act(async () => find('older').click());
      await act(async () => {
        if (signal === 'same-tab-credentials') notifyOrganizationCredentialChange();
        if (signal === 'other-tab-credentials') {
          const channel = new FakeBroadcastChannel('jiuwen:organization-credentials');
          channel.postMessage('changed');
          channel.close();
        }
        if (signal === 'disconnect') webClient.updateState('closed');
        if (signal === 'pagehide') window.dispatchEvent(new window.Event('pagehide'));
        if (signal === 'hidden') {
          Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
          document.dispatchEvent(new window.Event('visibilitychange'));
        }
      });
      assert.equal(find('text'), null);
      assert.equal(find('older'), null);
      await act(async () => resolve(page('late', 'stale')));
      await tick();
      assert.equal(find('text'), null);
      assert.equal(find('older'), null);
    } finally {
      await unmount();
    }
  });
}

test('strict effect replay still completes a current read', async () => {
  sessionSharingApi.history = async () => page();
  root = createRoot(document.getElementById('root'));
  await act(async () =>
    root.render(
      React.createElement(
        React.StrictMode,
        null,
        React.createElement(SharedHistoryDialog, { target, onClose: () => {} }),
      ),
    ),
  );
  await tick();
  try {
    assert.equal(find('text').textContent, 'private text');
  } finally {
    await unmount();
  }
});

test('restricted BroadcastChannel still clears local content and does not block successful auth', async () => {
  sessionSharingApi.history = async () => page();
  await mount();
  try {
    globalThis.BroadcastChannel = class {
      constructor() {
        throw new Error('restricted');
      }
    };
    await act(async () => notifyOrganizationCredentialChange());
    assert.equal(find('text'), null);
    assert.ok(find('error'));
  } finally {
    globalThis.BroadcastChannel = FakeBroadcastChannel;
    await unmount();
  }
});

// Manual clock for this component's bounded timers; request/tick microtasks stay real.
function clock() {
  const originalSet = globalThis.setTimeout;
  const originalClear = globalThis.clearTimeout;
  const timers = new Map();
  let next = 1;
  globalThis.setTimeout = (callback, delay, ...args) => {
    if (delay > 0 && delay <= 15_000) {
      const id = { id: next++ };
      timers.set(id, { callback: () => callback(...args), delay });
      return id;
    }
    return originalSet(callback, delay, ...args);
  };
  globalThis.clearTimeout = (id) => {
    if (!timers.delete(id)) originalClear(id);
  };
  return {
    timers,
    async fire() {
      const [id, entry] = timers.entries().next().value;
      timers.delete(id);
      await act(async () => entry.callback());
      await tick();
    },
    restore() {
      globalThis.setTimeout = originalSet;
      globalThis.clearTimeout = originalClear;
    },
  };
}

test('viewGrant validates exact unique active view target and bounded server expiry; never sends identity', async () => {
  const original = webClient.request;
  const good = { ...target, state: 'active', actions: ['view'], revision: 2, expires_at: Date.now() / 1000 + 60 };
  try {
    let sent;
    webClient.request = async (...args) => {
      sent = args;
      return { shares: [good] };
    };
    assert.deepEqual(await originalViewGrant({ ...target, actor_id: 'alice' }), {
      revision: 2,
      expires_at: good.expires_at,
    });
    assert.equal(sent[0], 'session.share.list');
    assert.deepEqual(sent[1], {});
    for (const shares of [
      [],
      [good, good],
      [{ ...good, session_id: 'other' }],
      [{ ...good, share_id: 'other' }],
      [{ ...good, state: 'unavailable' }],
      [{ ...good, actions: ['execute'] }],
      [{ ...good, revision: 0 }],
      [{ ...good, revision: 1.5 }],
      [{ ...good, expires_at: undefined }],
      [{ ...good, expires_at: NaN }],
      [{ ...good, expires_at: Infinity }],
      [{ ...good, expires_at: Date.now() / 1000 - 1 }],
    ]) {
      webClient.request = async () => ({ shares });
      await assert.rejects(originalViewGrant(target), /Shared history unavailable/);
    }
    webClient.request = async () => ({ shares: [{ ...good, expires_at: null }] });
    assert.equal((await originalViewGrant(target)).expires_at, null);
  } finally {
    webClient.request = original;
  }
});

test('visible periodic reauthorization withdraws revoked cached data without a push', async () => {
  const c = clock();
  let grants = 0;
  let histories = 0;
  sessionSharingApi.viewGrant = async () => {
    if (++grants > 1) throw Error('revoked private detail');
    return { revision: 1, expires_at: null };
  };
  sessionSharingApi.history = async () => {
    histories++;
    return page('visible-private', 'cursor');
  };
  await mount();
  try {
    assert.equal(find('text').textContent, 'visible-private');
    assert.equal([...c.timers.values()][0].delay, 15000);
    await c.fire();
    assert.equal(find('text'), null);
    assert.equal(find('older'), null);
    assert.ok(find('error'));
    assert.equal(histories, 1);
    assert.equal(grants, 2);
    assert.equal(c.timers.size, 0);
    assert.ok(!document.body.textContent.includes('revoked private detail'));
  } finally {
    await unmount();
    c.restore();
  }
});

test('expiry clears pending old page, aborts it, and late response cannot refill', async () => {
  const c = clock();
  let grants = 0;
  let oldResolve;
  let oldSignal;
  sessionSharingApi.viewGrant = async () => {
    if (++grants > 2) throw Error('expired');
    return { revision: 1, expires_at: Date.now() / 1000 + 1 };
  };
  sessionSharingApi.history = async (_t, cursor, signal) =>
    cursor
      ? new Promise((resolve) => {
          oldResolve = resolve;
          oldSignal = signal;
        })
      : page('private', 'old');
  await mount();
  try {
    await act(async () => find('older').click());
    assert.ok([...c.timers.values()][0].delay <= 1000);
    await c.fire();
    assert.equal(oldSignal.aborted, true);
    assert.equal(find('text'), null);
    assert.ok(find('error'));
    await act(async () => oldResolve(page('too late', 'stale')));
    await tick();
    assert.equal(find('text'), null);
    assert.equal(find('older'), null);
  } finally {
    await unmount();
    c.restore();
  }
});

test('grant version change discards old cursor and refetches the first authorized page', async () => {
  let revision = 1;
  const cursors = [];
  sessionSharingApi.viewGrant = async () => ({ revision, expires_at: null });
  sessionSharingApi.history = async (_t, cursor) => {
    cursors.push(cursor);
    return page(`revision-${revision}`, 'cursor');
  };
  await mount();
  try {
    revision = 2;
    await act(async () => find('older').click());
    await tick();
    assert.deepEqual(cursors, [undefined, undefined]);
    assert.equal(document.querySelectorAll('[data-testid="multi-session-shared-history-message"]').length, 1);
    assert.equal(find('text').textContent, 'revision-2');
  } finally {
    await unmount();
  }
});

for (const stop of ['close', 'unmount', 'identity', 'disconnect', 'hidden']) {
  test(`${stop} cancels timer and pending grant; old response and ready do not bypass invalidation`, async () => {
    const c = clock();
    let calls = 0;
    let resolve;
    let signal;
    sessionSharingApi.viewGrant = async (_t, s) => {
      calls++;
      if (calls === 1) return { revision: 1, expires_at: null };
      signal = s;
      return new Promise((r) => {
        resolve = r;
      });
    };
    sessionSharingApi.history = async () => page('cached', 'c');
    await mount();
    let unmounted = false;
    try {
      await act(async () => find('older').click());
      await act(async () => {
        if (stop === 'close') find('close').click();
        if (stop === 'identity') notifyOrganizationCredentialChange();
        if (stop === 'disconnect') webClient.updateState('closed');
        if (stop === 'hidden') {
          Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
          document.dispatchEvent(new window.Event('visibilitychange'));
        }
      });
      if (stop === 'unmount') {
        await unmount();
        unmounted = true;
      }
      assert.equal(signal.aborted, true);
      assert.equal(c.timers.size, 0);
      await act(async () => resolve({ revision: 1, expires_at: null }));
      await tick();
      assert.equal(find('text'), null);
      assert.equal(calls, 2);
      if (stop === 'identity' || stop === 'close') {
        await act(async () => webClient.updateState('ready'));
        await tick();
        assert.equal(calls, 2);
      }
    } finally {
      if (!unmounted) await unmount();
      c.restore();
    }
  });
}

test('history arriving after the reported expiry is rejected even before a delayed timer runs', async () => {
  const c = clock();
  const originalNow = Date.now;
  const now = originalNow();
  let resolve;
  sessionSharingApi.viewGrant = async () => ({ revision: 1, expires_at: (now + 500) / 1000 });
  sessionSharingApi.history = () =>
    new Promise((r) => {
      resolve = r;
    });
  await mount();
  try {
    Date.now = () => now + 501;
    await act(async () => resolve(page('expired delivery')));
    await tick();
    assert.equal(find('text'), null);
    assert.ok(find('error'));
    assert.equal(c.timers.size, 0);
  } finally {
    Date.now = originalNow;
    await unmount();
    c.restore();
  }
});

test('hidden viewer sends no requests; becoming visible reauthorizes through both APIs', async () => {
  let grants = 0,
    histories = 0;
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
  sessionSharingApi.viewGrant = async () => {
    grants++;
    return { revision: 1, expires_at: null };
  };
  sessionSharingApi.history = async () => {
    histories++;
    return page();
  };
  await mount();
  try {
    assert.equal(grants, 0);
    assert.equal(histories, 0);
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
    await act(async () => document.dispatchEvent(new window.Event('visibilitychange')));
    await tick();
    assert.equal(grants, 1);
    assert.equal(histories, 1);
    assert.ok(find('text'));
  } finally {
    await unmount();
  }
});
