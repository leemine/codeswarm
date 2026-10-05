import test from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
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
if (!globalThis.crypto) globalThis.crypto = webcrypto;
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
const { sessionSharingApi, continuationRequest, validateContinuedSession } = await import(
  base + 'services/sessionSharingApi.js'
);
const { projectRegistryClient } = await import(base + 'features/workspace/projectRegistryClient.js');
const { webClient } = await import(base + 'services/webClient.js');
const { notifyOrganizationCredentialChange } = await import(base + 'services/organizationCredentialEvents.js');
const { newContinuationAttempt, isContinuationOutcomeUnknown, prepareContinuedConversation } = await import(
  base + 'multi-session/state/continueSharedSession.js'
);
const originalApi = { ...sessionSharingApi };
const originalList = projectRegistryClient.list;
const originalRequest = webClient.request;
const share = {
  session_id: 'source',
  share_id: 'share',
  revision: 7,
  state: 'active',
  actions: ['view', 'execute'],
  can_update: false,
  can_revoke: false,
};
const option = {
  execution_profile_id: 'host-non-default',
  provider_id: 'native',
  mode: 'agent.code.normal',
  model_name: 'same-name#3',
  label: 'Host profile · Model 3',
};
const input = {
  session_id: 'source',
  share_id: 'share',
  expected_revision: 7,
  target_project_id: 'recipient-project',
  create_token: 'token',
  execution_profile_id: option.execution_profile_id,
  model_name: option.model_name,
  mode: option.mode,
  title: 'Private title',
};
const result = {
  session_id: 'new-private',
  project_id: input.target_project_id,
  project_dir: '/recipient',
  work_mode: 'code',
  mode: input.mode,
  execution_profile_id: input.execution_profile_id,
  model_name: input.model_name,
  title: input.title,
  persist_session: true,
  continued_from: { session_id: input.session_id, share_id: input.share_id, revision: input.expected_revision },
};
const find = (name) => document.querySelector(`[data-testid="${name}"]`);
const tick = () =>
  act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
const click = async (id) => {
  await act(async () => find(id).click());
  await tick();
};
const select = async (id, value) => {
  await act(async () => {
    find(id).value = value;
    find(id).dispatchEvent(new dom.window.Event('change', { bubbles: true }));
  });
  await tick();
};
let root;
async function mount(extra = {}) {
  webClient.state = 'ready';
  sessionSharingApi.list = async () => ({ shares: [share] });
  projectRegistryClient.list = async () => ({
    projects: [
      { project_id: 'recipient-project', name: 'Recipient project' },
      { project_id: 'other', name: 'Other project' },
    ],
  });
  sessionSharingApi.continuationOptions = async () => [option];
  root = createRoot(document.getElementById('root'));
  await act(async () =>
    root.render(React.createElement(ShareSessionDialog, { onClose: () => {}, onContinued: async () => {}, ...extra })),
  );
  await tick();
}
async function unmount() {
  await act(async () => root.unmount());
  Object.assign(sessionSharingApi, originalApi);
  projectRegistryClient.list = originalList;
  webClient.request = originalRequest;
}
async function choose() {
  await click('multi-session-sharing-continue');
  await select('multi-session-continuation-project', 'recipient-project');
}
const responseFor = (request) => ({ ...result, title: request.title || 'Private conversation' });

test('options exact wire method and tuple whitelist; injected secret/unknown rows rejected', async () => {
  const original = webClient.request;
  const calls = [];
  const params = {
    session_id: 'source',
    share_id: 'share',
    expected_revision: 7,
    target_project_id: 'recipient-project',
  };
  let response = { ...params, options: [option] };
  webClient.request = async (...args) => {
    calls.push(args);
    return response;
  };
  try {
    assert.deepEqual(await sessionSharingApi.continuationOptions({ ...params, credentials: 'never' }), [option]);
    assert.equal(calls[0][0], 'session.share.continuation.options');
    assert.deepEqual(calls[0][1], params);
    const openCode = { ...option, execution_profile_id: 'host-opencode', provider_id: 'opencode' };
    response = { ...params, options: [option, openCode] };
    assert.deepEqual(await sessionSharingApi.continuationOptions(params), [option, openCode]);
    for (const patch of [
      { provider_id: 'codex' },
      { provider_id: 'unknown' },
      { model_name: 'same-name' },
      { api_key: 'synthetic-secret' },
      { mode: 'team.work.normal' },
    ]) {
      response = { ...params, options: [{ ...option, ...patch }] };
      await assert.rejects(sessionSharingApi.continuationOptions(params));
    }
    response = { ...params, options: [] };
    assert.deepEqual(await sessionSharingApi.continuationOptions(params), []);
    response = { ...params, target_project_id: 'other', options: [option] };
    await assert.rejects(sessionSharingApi.continuationOptions(params));
    response = { ...params, options: [option, option] };
    await assert.rejects(sessionSharingApi.continuationOptions(params));
  } finally {
    webClient.request = original;
  }
});

test('original selector decodes and submits an explicit OpenCode option through actual APIs', async () => {
  const openCode = { ...option, execution_profile_id: 'host-opencode', provider_id: 'opencode' };
  const calls = [];
  let opened;
  await mount({
    onContinued: async (...args) => {
      opened = args;
    },
  });
  sessionSharingApi.continuationOptions = originalApi.continuationOptions;
  webClient.request = async (method, params) => {
    calls.push({ method, params });
    if (method === 'session.share.continuation.options') return { ...params, options: [openCode] };
    assert.equal(method, 'session.share.continue');
    return { ...responseFor(params), execution_profile_id: openCode.execution_profile_id };
  };
  try {
    await choose();
    assert.equal(find('multi-session-continuation-error'), null);
    assert.equal(
      find('multi-session-continuation-option').value,
      JSON.stringify([openCode.execution_profile_id, openCode.mode, openCode.model_name]),
    );
    assert.equal(find('multi-session-continuation-submit').disabled, false);
    await click('multi-session-continuation-submit');
    assert.deepEqual(
      calls.map(({ method }) => method),
      ['session.share.continuation.options', 'session.share.continue'],
    );
    assert.equal(calls[1].params.execution_profile_id, openCode.execution_profile_id);
    assert.equal(calls[1].params.model_name, openCode.model_name);
    assert.equal(opened[0].session_id, 'new-private');
    assert.equal(opened[1].execution_profile_id, openCode.execution_profile_id);
  } finally {
    await unmount();
  }
});

test('continue RPC sends one immutable exact input; validates original source and chosen tuple', async () => {
  const original = webClient.request;
  let sent;
  webClient.request = async (...args) => {
    sent = args;
    return result;
  };
  try {
    assert.equal(await sessionSharingApi.continueSession({ ...input, api_key: 'never', identity: 'forged' }), result);
    assert.equal(sent[0], 'session.share.continue');
    assert.deepEqual(sent[1], input);
    assert.equal(Object.isFrozen(sent[1]), true);
    assert.equal(sent[2].timeoutMs, 60000);
    for (const patch of [
      { session_id: 'source' },
      { project_id: 'other' },
      { mode: 'agent.work.normal' },
      { model_name: 'same-name#0' },
      { execution_profile_id: 'default' },
      { persist_session: false },
      { title: 'different' },
      { credential_reference: 'hidden' },
      { continued_from: { ...result.continued_from, revision: 8 } },
    ]) {
      assert.throws(() => validateContinuedSession({ ...result, ...patch }, input));
    }
    assert.throws(() => continuationRequest({ ...input, model_name: 'alias' }));
  } finally {
    webClient.request = original;
  }
});

test('token is random immutable memory-only; only transport/timeout outcomes are unknown', () => {
  const first = newContinuationAttempt(input);
  const second = newContinuationAttempt(input);
  assert.notEqual(first.create_token, second.create_token);
  assert.equal(Object.isFrozen(first), true);
  for (const code of ['REQUEST_TIMEOUT', 'AGENT_SERVER_TIMEOUT', 'WS_CLOSED', 'WS_DISCONNECTED', 'WS_NOT_READY'])
    assert.equal(isContinuationOutcomeUnknown({ code }), true);
  for (const code of ['FORBIDDEN', 'CONFLICT', 'BAD_REQUEST'])
    assert.equal(isContinuationOutcomeUnknown({ code }), false);
});

test('owned metadata and switch are mandatory; sanitize metadata without source equipment', async () => {
  const calls = [];
  const metadata = {
    ...result,
    model: input.model_name,
    created_at: 'now',
    updated_at: 'now',
    session_equipment: { plugin_names: ['source-plugin'] },
    tools: ['source-tool'],
  };
  const request = async (method, params) => {
    calls.push({ method, params });
    return method === 'session.switch'
      ? { session_id: result.session_id, mode: result.mode, switched: true }
      : metadata;
  };
  const session = await prepareContinuedConversation(request, result, input, () => true, {
    session_id: 'current',
    mode: 'agent',
    view_id: 'view',
  });
  assert.deepEqual(
    calls.map((call) => call.method),
    ['session.get_metadata', 'session.switch'],
  );
  assert.equal(session.is_processing, false);
  assert.equal(session.session_equipment, undefined);
  assert.equal(session.tools, undefined);
  assert.equal(session.session_id, 'new-private');
  for (const patch of [
    { model: 'same-name#0' },
    { project_dir: '/source' },
    { execution_profile_id: 'other' },
    { is_processing: true },
  ]) {
    await assert.rejects(
      prepareContinuedConversation(
        async () => ({ ...metadata, ...patch }),
        result,
        input,
        () => true,
        {},
      ),
    );
  }
  await assert.rejects(
    prepareContinuedConversation(
      async (method) => {
        if (method === 'session.switch') throw { code: 'FORBIDDEN' };
        return metadata;
      },
      result,
      input,
      () => true,
      {},
    ),
  );
  let current = true;
  await assert.rejects(
    prepareContinuedConversation(
      async () => {
        current = false;
        return metadata;
      },
      result,
      input,
      () => current,
      {},
    ),
  );
});

test('received view AND execute entry only, keeps read-only viewer and no management privilege', async () => {
  await mount();
  try {
    assert.ok(find('multi-session-sharing-continue'));
    assert.equal(find('multi-session-sharing-form'), null);
  } finally {
    await unmount();
  }
  for (const actions of [['view'], ['execute'], []]) {
    await mount();
    sessionSharingApi.list = async () => ({ shares: [{ ...share, actions }] });
    await click('multi-session-sharing-refresh');
    assert.equal(find('multi-session-sharing-continue'), null);
    await unmount();
  }
});

test('project first, empty trusted options cannot create; explicit returned tuple used', async () => {
  let submitted, opened;
  await mount({
    onContinued: async (...args) => {
      opened = args;
    },
  });
  sessionSharingApi.continueSession = async (request) => {
    submitted = request;
    return responseFor(request);
  };
  try {
    await choose();
    assert.equal(find('multi-session-continuation-submit').disabled, false);
    await click('multi-session-continuation-submit');
    assert.equal(submitted.target_project_id, 'recipient-project');
    assert.equal(submitted.execution_profile_id, 'host-non-default');
    assert.equal(submitted.model_name, 'same-name#3');
    assert.deepEqual(Object.keys(submitted).sort(), Object.keys(input).sort());
    assert.equal(opened[0].session_id, 'new-private');
    assert.equal(opened[1], submitted);
  } finally {
    await unmount();
  }
  await mount();
  sessionSharingApi.continuationOptions = async () => [];
  try {
    await choose();
    assert.ok(find('multi-session-continuation-empty'));
    assert.equal(find('multi-session-continuation-submit').disabled, true);
  } finally {
    await unmount();
  }
});

for (const code of ['REQUEST_TIMEOUT', 'AGENT_SERVER_TIMEOUT'])
  test(`${code}: manual retry retains exact token and immutable input`, async () => {
    const calls = [];
    await mount();
    sessionSharingApi.continueSession = async (request) => {
      calls.push(request);
      throw { code };
    };
    try {
      await choose();
      await click('multi-session-continuation-submit');
      assert.equal(find('multi-session-continuation-error').dataset.variant, 'unknown');
      assert.equal(find('multi-session-continuation-project').disabled, true);
      await click('multi-session-continuation-submit');
      assert.equal(calls.length, 2);
      assert.equal(calls[0], calls[1]);
      await click('multi-session-continuation-new-input');
      await select('multi-session-continuation-project', 'recipient-project');
      await click('multi-session-continuation-submit');
      assert.notEqual(calls[2].create_token, calls[0].create_token);
    } finally {
      await unmount();
    }
  });

for (const code of ['FORBIDDEN', 'CONFLICT'])
  test(`${code}: no automatic or same-attempt retry`, async () => {
    let calls = 0;
    await mount();
    sessionSharingApi.continueSession = async () => {
      calls++;
      throw { code };
    };
    try {
      await choose();
      await click('multi-session-continuation-submit');
      await tick();
      assert.equal(calls, 1);
      assert.equal(find('multi-session-continuation-submit').disabled, true);
      assert.equal(find('multi-session-continuation-error').dataset.variant, 'failed');
    } finally {
      await unmount();
    }
  });

for (const invalidation of ['close', 'auth', 'disconnect'])
  test(`${invalidation} ignores late create; disconnect permits exact-input retry`, async () => {
    let resolveLate,
      opened = 0;
    const calls = [];
    await mount({
      onContinued: async () => {
        opened++;
      },
    });
    sessionSharingApi.continueSession = async (request) => {
      calls.push(request);
      return new Promise((resolve) => {
        resolveLate = () => resolve(responseFor(request));
      });
    };
    try {
      await choose();
      await click('multi-session-continuation-submit');
      if (invalidation === 'close') await click('multi-session-continuation-cancel');
      if (invalidation === 'auth') await act(async () => notifyOrganizationCredentialChange());
      if (invalidation === 'disconnect') await act(async () => webClient.updateState('reconnecting'));
      await act(async () => resolveLate());
      await tick();
      assert.equal(opened, 0);
      if (invalidation === 'disconnect') {
        await act(async () => webClient.updateState('ready'));
        sessionSharingApi.continueSession = async (request) => {
          calls.push(request);
          return responseFor(request);
        };
        await click('multi-session-continuation-submit');
        assert.equal(calls[0], calls[1]);
        assert.equal(opened, 1);
      }
    } finally {
      await unmount();
    }
  });

test('late options from prior project cannot replace new project options', async () => {
  let resolveOld;
  await mount();
  sessionSharingApi.continuationOptions = async ({ target_project_id }) =>
    target_project_id === 'recipient-project'
      ? new Promise((resolve) => {
          resolveOld = resolve;
        })
      : [];
  try {
    await choose();
    await select('multi-session-continuation-project', 'other');
    await act(async () => resolveOld([option]));
    await tick();
    assert.ok(find('multi-session-continuation-empty'));
    assert.equal(find('multi-session-continuation-submit').disabled, true);
    assert.equal(find('multi-session-continuation-project').value, 'other');
  } finally {
    await unmount();
  }
});

test('close and reopen retains uncertain immutable attempt until explicit new input; credential change clears it', async () => {
  const attempts = new Map();
  const calls = [];
  await mount({ continuationAttempts: attempts });
  sessionSharingApi.continueSession = async (request) => {
    calls.push(request);
    throw { code: 'REQUEST_TIMEOUT' };
  };
  await choose();
  await click('multi-session-continuation-submit');
  assert.equal(attempts.size, 1);
  await unmount();
  await mount({ continuationAttempts: attempts });
  sessionSharingApi.continueSession = async (request) => {
    calls.push(request);
    throw { code: 'REQUEST_TIMEOUT' };
  };
  try {
    await click('multi-session-sharing-continue');
    assert.ok(find('multi-session-continuation-saved-request'));
    assert.equal(find('multi-session-continuation-submit').dataset.variant, 'retry');
    await click('multi-session-continuation-submit');
    assert.equal(calls[0], calls[1]);
    await act(async () => notifyOrganizationCredentialChange());
    assert.equal(attempts.size, 0);
    assert.equal(find('multi-session-continuation-saved-request'), null);
  } finally {
    await unmount();
  }
});

test('late switch completion cannot produce local metadata after generation invalidation', async () => {
  let current = true;
  const metadata = { ...result, model: input.model_name, created_at: 'now', updated_at: 'now' };
  const request = async (method) => {
    if (method === 'session.switch') {
      current = false;
      return { session_id: result.session_id, mode: result.mode, switched: true };
    }
    return metadata;
  };
  await assert.rejects(
    prepareContinuedConversation(request, result, input, () => current, {
      session_id: 'current',
      mode: 'agent',
      view_id: 'view',
    }),
  );
});

test('owned metadata display timestamp compatibility does not relax execution identity checks', async () => {
  const metadata = { ...result, model: input.model_name, created_at: 123, updated_at: null };
  const session = await prepareContinuedConversation(
    async (method) =>
      method === 'session.switch' ? { session_id: result.session_id, mode: result.mode, switched: true } : metadata,
    result,
    input,
    () => true,
    { session_id: 'current', mode: 'agent', view_id: 'view' },
  );
  assert.equal(typeof session.created_at, 'string');
  assert.equal(typeof session.updated_at, 'string');
  assert.equal(session.model, input.model_name);
});

test('navigation remount permanently invalidates prior create callback while preserving retry input', async () => {
  let finish,
    opened = 0;
  const attempts = new Map();
  await mount({
    continuationAttempts: attempts,
    onContinued: async () => {
      opened++;
    },
  });
  sessionSharingApi.continueSession = async (request) =>
    new Promise((resolve) => {
      finish = () => resolve(responseFor(request));
    });
  try {
    await choose();
    await click('multi-session-continuation-submit');
    await act(async () =>
      root.render(
        React.createElement(ShareSessionDialog, {
          key: 'another-active-session',
          onClose: () => {},
          continuationAttempts: attempts,
          onContinued: async () => {
            opened++;
          },
        }),
      ),
    );
    await tick();
    await act(async () => finish());
    await tick();
    assert.equal(opened, 0);
    assert.equal(attempts.size, 1);
  } finally {
    await unmount();
  }
});

for (const mode of ['agent.work.normal', 'agent.code.normal']) {
  test(`switch confirms exact server canonical mode ${mode}`, async () => {
    const requestInput = { ...input, mode };
    const response = { ...result, mode, work_mode: mode === 'agent.work.normal' ? 'work' : 'code' };
    const session = await prepareContinuedConversation(
      async (method) =>
        method === 'session.switch'
          ? { session_id: response.session_id, mode, switched: true }
          : { ...response, model: input.model_name },
      response,
      requestInput,
      () => true,
      { session_id: 'current', mode: 'agent', view_id: 'view' },
    );
    assert.equal(session.mode, mode);
    assert.equal(session.is_processing, false);
  });
}

test('RPC success cannot confirm a failed, missing or mismatched switch payload', async () => {
  const valid = { session_id: result.session_id, mode: result.mode, switched: true };
  for (const payload of [
    null,
    undefined,
    {},
    [],
    'success',
    { ...valid, switched: false },
    { ...valid, switched: 'true' },
    { session_id: result.session_id, mode: result.mode },
    { ...valid, session_id: 'other-session' },
    { ...valid, mode: 'agent' },
    { ...valid, mode: 'code.normal' },
  ]) {
    await assert.rejects(
      prepareContinuedConversation(
        async (method) => (method === 'session.switch' ? payload : { ...result, model: input.model_name }),
        result,
        input,
        () => true,
        { session_id: 'current', mode: 'agent', view_id: 'view' },
      ),
      /session switch was not confirmed/,
    );
  }
});
