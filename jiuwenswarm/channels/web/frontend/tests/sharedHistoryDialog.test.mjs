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
        if (signal === 'hidden') document.dispatchEvent(new window.Event('visibilitychange'));
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
