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
const { ShareSessionDialog } =
  await import('../node_modules/.cache/session-sharing/multi-session/dialogs/ShareSessionDialog.js');
const { sessionSharingApi } = await import('../node_modules/.cache/session-sharing/services/sessionSharingApi.js');
const { webClient } = await import('../node_modules/.cache/session-sharing/services/webClient.js');
const tick = () =>
  act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
const find = (id) => document.querySelector(`[data-testid="${id}"]`);
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
const originals = { ...sessionSharingApi };
let root;
async function mount(props = {}) {
  root = createRoot(document.getElementById('root'));
  await act(async () =>
    root.render(React.createElement(ShareSessionDialog, { sessionId: 'session', onClose: () => {}, ...props })),
  );
  await tick();
}
async function unmount() {
  await act(async () => root.unmount());
  Object.assign(sessionSharingApi, originals);
}

test('RPC client uses existing websocket and only whitelisted fields', async () => {
  const original = webClient.request;
  const calls = [];
  webClient.request = async (method, params) => {
    calls.push({ method, params });
    return {};
  };
  try {
    const bounds = {
      actions: ['view'],
      expires_at: 12345,
      authority: 'spoof',
      history: { end: 999 },
      actor_id: 'spoof',
    };
    await sessionSharingApi.list();
    await sessionSharingApi.list('session');
    await sessionSharingApi.create('session', 'bob', bounds);
    await sessionSharingApi.update(share, bounds);
    await sessionSharingApi.revoke(share);
    assert.deepEqual(
      calls.map((c) => c.method),
      [
        'session.share.list',
        'session.share.list',
        'session.share.create',
        'session.share.update',
        'session.share.revoke',
      ],
    );
    assert.deepEqual(calls[2].params, {
      session_id: 'session',
      target_actor: 'bob',
      history_scope: 'current_snapshot',
      actions: ['view'],
      expires_at: 12345,
    });
    assert.equal(calls[3].params.expected_revision, 3);
    assert.equal(calls[3].params.history_scope, undefined);
    for (const call of calls)
      for (const field of ['authority', 'history', 'actor_id', 'subject_id', 'identity'])
        assert.equal(call.params[field], undefined);
  } finally {
    webClient.request = original;
  }
});

test('editor keeps actions independent and never expands the snapshot', async () => {
  sessionSharingApi.list = async (id) => ({ shares: id ? [share] : [] });
  let submitted;
  sessionSharingApi.update = async (item, bounds) => {
    submitted = { item, bounds };
    return { share: { ...item, revision: 4 } };
  };
  await mount();
  try {
    assert.equal(document.querySelectorAll('[data-testid="multi-session-sharing-action"]').length, 6);
    await act(async () => find('multi-session-sharing-edit').click());
    assert.equal(find('multi-session-sharing-target').value, 'bob');
    assert.equal(find('multi-session-sharing-target').disabled, true);
    const execute = document.querySelector('[data-testid="multi-session-sharing-action"][data-variant="execute"]');
    await act(async () => execute.click());
    assert.equal(
      document.querySelector('[data-variant="approve"] input')?.checked ??
        document.querySelector('input[data-variant="approve"]').checked,
      false,
    );
    await act(async () =>
      find('multi-session-sharing-form').dispatchEvent(
        new dom.window.Event('submit', { bubbles: true, cancelable: true }),
      ),
    );
    await tick();
    assert.deepEqual(submitted.bounds.actions, ['view', 'execute']);
    assert.equal(submitted.item.revision, 3);
    assert.equal(submitted.bounds.history_scope, undefined);
  } finally {
    await unmount();
  }
});

test('revoke denial clears cached grants and offers explicit refresh', async () => {
  sessionSharingApi.list = async (id) => ({ shares: id ? [share] : [] });
  sessionSharingApi.revoke = async () => {
    throw new Error('FORBIDDEN');
  };
  await mount();
  try {
    await act(async () => find('multi-session-sharing-revoke').click());
    await tick();
    assert.ok(find('multi-session-sharing-error'));
    assert.equal(find('multi-session-sharing-managed-item'), null);
    assert.equal(find('multi-session-sharing-form'), null);
    assert.equal(find('multi-session-sharing-refresh').disabled, false);
  } finally {
    await unmount();
  }
});

test('received share opens only the constrained callback without legacy switching', async () => {
  const incoming = { ...share, can_update: false, can_revoke: false };
  sessionSharingApi.list = async (id) => {
    if (id) throw Object.assign(new Error('denied'), { code: 'FORBIDDEN' });
    return { shares: [incoming] };
  };
  let opened;
  await mount({
    onOpenSharedSession: (target) => {
      opened = target;
    },
  });
  try {
    assert.equal(find('multi-session-sharing-form'), null);
    await act(async () => find('multi-session-sharing-open').click());
    assert.deepEqual(opened, { session_id: 'session', share_id: 'grant' });
    assert.equal(window.location.pathname, '/');
  } finally {
    await unmount();
  }
});

test('late list response cannot repopulate a different session', async () => {
  let resolveOld;
  sessionSharingApi.list = async (id) =>
    id === 'session'
      ? new Promise((resolve) => {
          resolveOld = resolve;
        })
      : { shares: [] };
  await mount();
  try {
    await act(async () =>
      root.render(React.createElement(ShareSessionDialog, { sessionId: 'other', onClose: () => {} })),
    );
    await tick();
    await act(async () => resolveOld({ shares: [share] }));
    await tick();
    assert.equal(find('multi-session-sharing-managed-item'), null);
  } finally {
    await unmount();
  }
});

test('inbox mode discovers shares without creating or selecting a Session', async () => {
  const calls = [];
  sessionSharingApi.list = async (id) => {
    calls.push(id);
    return { shares: [{ ...share, can_update: false, can_revoke: false }] };
  };
  await mount({ sessionId: undefined });
  try {
    assert.deepEqual(calls, [undefined]);
    assert.equal(find('multi-session-sharing-form'), null);
    assert.equal(find('multi-session-sharing-managed-list'), null);
    assert.ok(find('multi-session-sharing-received-item'));
    assert.equal(find('multi-session-sharing-open'), null);
  } finally {
    await unmount();
  }
});

test('execute-only incoming grant does not enable viewing', async () => {
  sessionSharingApi.list = async () => ({
    shares: [{ ...share, actions: ['execute'], can_update: false, can_revoke: false }],
  });
  await mount({ sessionId: undefined, onOpenSharedSession: () => assert.fail('view callback must be absent') });
  try {
    assert.ok(find('multi-session-sharing-received-item'));
    assert.equal(find('multi-session-sharing-open'), null);
  } finally {
    await unmount();
  }
});

test('refresh after expiry or revocation removes received items and explains the empty inbox', async () => {
  let shares = [{ ...share, can_update: false, can_revoke: false }];
  sessionSharingApi.list = async () => ({ shares });
  await mount({ sessionId: undefined, onOpenSharedSession: () => {} });
  try {
    assert.ok(find('multi-session-sharing-received-expiry'));
    assert.equal(find('multi-session-sharing-received-empty'), null);
    shares = [];
    await act(async () => find('multi-session-sharing-refresh').click());
    await tick();
    assert.equal(find('multi-session-sharing-received-item'), null);
    assert.equal(find('multi-session-sharing-open'), null);
    assert.ok(find('multi-session-sharing-received-empty'));
  } finally {
    await unmount();
  }
});

test('management authorization denial is visible even when the inbox succeeds', async () => {
  sessionSharingApi.list = async (id) => {
    if (id) throw Object.assign(new Error('denied'), { code: 'FORBIDDEN' });
    return { shares: [] };
  };
  await mount();
  try {
    assert.ok(find('multi-session-sharing-error'));
    assert.equal(find('multi-session-sharing-form'), null);
    assert.equal(find('multi-session-sharing-received-empty'), null);
  } finally {
    await unmount();
  }
});

test('inbox failure displays an error rather than claiming no received shares', async () => {
  sessionSharingApi.list = async () => {
    throw new Error('FORBIDDEN');
  };
  await mount({ sessionId: undefined });
  try {
    assert.ok(find('multi-session-sharing-error'));
    assert.equal(find('multi-session-sharing-received-empty'), null);
  } finally {
    await unmount();
  }
});
