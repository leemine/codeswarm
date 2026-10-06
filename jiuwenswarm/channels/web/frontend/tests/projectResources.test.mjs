import test from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/' });
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  sessionStorage: dom.window.sessionStorage,
  localStorage: dom.window.localStorage,
  CustomEvent: dom.window.CustomEvent,
  IS_REACT_ACT_ENVIRONMENT: true,
  BroadcastChannel: undefined,
});
dom.window.HTMLDialogElement.prototype.showModal = function () {
  this.open = true;
};
dom.window.HTMLDialogElement.prototype.close = function () {
  this.open = false;
};
const { default: React, act } = await import('react');
const { createRoot } = await import('react-dom/client');
const { default: i18next } = await import('i18next');
const { initReactI18next } = await import('react-i18next');
await i18next
  .use(initReactI18next)
  .init({ lng: 'en', showSupportNotice: false, resources: { en: { translation: {} } } });
const { ProjectContentDialog } =
  await import('../node_modules/.cache/project-content/multi-session/sidebar/ProjectContentDialog.js');
const { projectRegistryClient: client } =
  await import('../node_modules/.cache/project-content/features/workspace/projectRegistryClient.js');
const { ResourceMutationCommittedError } =
  await import('../node_modules/.cache/project-content/features/workspace/projectResourceClient.js');
const { webClient } = await import('../node_modules/.cache/project-content/services/webClient.js');
const { notifyOrganizationCredentialChange } =
  await import('../node_modules/.cache/project-content/services/organizationCredentialEvents.js');
const root = createRoot(document.getElementById('root'));
const find = (id) => document.querySelector(`[data-testid="multi-session-project-resources-${id}"]`);
const tick = () =>
  act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
const page = () => ({
  project_id: 'p',
  acl_revision: 2,
  resource_revision: 4,
  resources: [
    {
      resource_id: 'workspace',
      kind: 'workspace',
      actions: ['read'],
      can_grant: true,
      expires_at: null,
      grants: [{ target_actor: 'bob', actions: ['read'], expires_at: null, can_revoke: true, state: 'active' }],
    },
  ],
});
const input = {
  project_id: 'p',
  resource_id: 'workspace',
  target_actor: 'bob',
  expected_acl_revision: 2,
  expected_resource_revision: 4,
};
const receipt = {
  mutation: { committed: true, project_id: 'p', resource_id: 'workspace', target_actor: 'bob', resource_revision: 5 },
};
const content = {
  project_id: 'p',
  revision: 1,
  latest_revision: 1,
  can_write: true,
  instructions: 'Keep this draft',
  sources: [],
  versions: [{ revision: 1, updated_at: 1 }],
};
let calls, stateListener;
function mock(handler = () => page()) {
  calls = [];
  webClient.request = async (method, params, options = {}) => {
    calls.push({ method, params, options });
    options.onRequestId?.('original-rpc');
    if (method === 'project.content.get') return { ...content, project_id: params.project_id };
    return handler(method, params, options);
  };
  webClient.onStateChange = (handler) => {
    stateListener = handler;
    return () => {
      stateListener = undefined;
    };
  };
}
async function mount(projectId = 'p') {
  await act(async () =>
    root.render(
      React.createElement(ProjectContentDialog, {
        project: { project_id: projectId, name: projectId },
        resourcesEnabled: true,
        onClose() {},
      }),
    ),
  );
  await tick();
  await act(async () => document.querySelector('[data-testid="multi-session-project-tab-resources"]').click());
  await tick();
}
async function unmount() {
  await act(async () => root.render(null));
}
async function change(id, value) {
  await act(async () => {
    const el = find(id);
    const prototype =
      el.tagName === 'SELECT' ? dom.window.HTMLSelectElement.prototype : dom.window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, 'value').set.call(el, value);
    el.dispatchEvent(new dom.window.Event(el.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
  });
}
function exitError(
  payload = { code: 'EXIT_UNCONFIRMED', error: 'private server text', ...receipt, exit_confirmed: false },
  requestId = 'original-rpc',
) {
  return Object.assign(new Error('private server text'), { code: 'EXIT_UNCONFIRMED', requestId, payload });
}

test('strict list rejects extra secret fields, malformed capabilities, duplicates and wrong project', async () => {
  mock();
  assert.deepEqual(await client.listResources('p'), page());
  const invalid = [
    (x) => {
      x.project_id = 'other';
    },
    (x) => {
      x.reference = '/secret';
    },
    (x) => {
      x.resources[0].grants[0].api_key = 'secret';
    },
    (x) => {
      x.resources[0].actions = ['execute'];
    },
    (x) => {
      x.resources[0].can_grant = 'true';
    },
    (x) => {
      x.resources.push(x.resources[0]);
    },
    (x) => {
      x.resources[0].grants.push(x.resources[0].grants[0]);
    },
    (x) => {
      x.resources[0].grants[0].state = 'revoked';
    },
    (x) => {
      x.resources[0].grants[0].actions = []; // Active grants require at least one action.
    },
    (x) => {
      x.acl_revision = -1;
    },
  ];
  for (const damage of invalid) {
    const value = page();
    damage(value);
    mock(() => value);
    await assert.rejects(client.listResources('p'));
  }
});

test('grant/revoke carry exact revision inputs and reject unapproved fields and wrong receipts', async () => {
  mock(() => receipt);
  const grant = { ...input, actions: ['read'], expires_at: null };
  await client.grantResource(grant);
  await client.revokeResource(input);
  assert.deepEqual(
    calls.map(({ method, params }) => ({ method, params })),
    [
      { method: 'project.resources.grant', params: grant },
      { method: 'project.resources.revoke', params: input },
    ],
  );
  for (const extra of [{ scope: '/tmp' }, { delegable: true }, { subject_id: 'other' }])
    await assert.rejects(client.grantResource({ ...grant, ...extra }));
  for (const patch of [
    { target_actor: 'alice' },
    { resource_id: 'other' },
    { project_id: 'other' },
    { resource_revision: 6 },
    { committed: false },
  ]) {
    mock(() => ({ mutation: { ...receipt.mutation, ...patch } }));
    await assert.rejects(client.revokeResource(input));
  }
});

test('only exact original RPC EXIT_UNCONFIRMED becomes a committed mutation', async () => {
  mock(() => {
    throw exitError();
  });
  await assert.rejects(client.revokeResource(input), ResourceMutationCommittedError);
  const errors = [
    exitError(undefined, 'other-rpc'),
    exitError({ ...receipt, code: 'OTHER', exit_confirmed: false }),
    exitError({ ...receipt, code: 'EXIT_UNCONFIRMED', exit_confirmed: true }),
    exitError({ ...receipt, code: 'EXIT_UNCONFIRMED', exit_confirmed: false, history: 'private' }),
    exitError({
      code: 'EXIT_UNCONFIRMED',
      exit_confirmed: false,
      mutation: { ...receipt.mutation, target_actor: 'alice' },
    }),
  ];
  for (const error of errors) {
    mock(() => {
      throw error;
    });
    await assert.rejects(client.revokeResource(input), (error) => !(error instanceof ResourceMutationCommittedError));
  }
});

test('actual dialog tabs preserve content and grant only selected action with inherited expiry', async () => {
  mock((method) => (method === 'project.resources.grant' ? receipt : page()));
  try {
    await mount();
    await change('select', 'workspace');
    await change('target', 'bob');
    await act(async () => find('action').click());
    assert.equal(find('submit').disabled, false);
    await act(async () => find('submit').click());
    await tick();
    assert.deepEqual(calls.find((call) => call.method === 'project.resources.grant').params, {
      ...input,
      actions: ['read'],
      expires_at: null,
    });
    assert.equal(find('notice').dataset.variant, 'saved');
    await act(async () => document.querySelector('[data-testid="multi-session-project-tab-content"]').click());
    assert.equal(
      document.querySelector('[data-testid="multi-session-project-content-instructions"]').value,
      'Keep this draft',
    );
    assert.equal(find('row'), null);
  } finally {
    await unmount();
  }
});

test('unavailable/root grant remains revocable without can_grant and never exposes delegation form', async () => {
  const value = page();
  Object.assign(value.resources[0], { can_grant: false, actions: [] });
  Object.assign(value.resources[0].grants[0], { state: 'unavailable', actions: [], expires_at: null });
  mock((method) => (method.endsWith('.revoke') ? receipt : value));
  try {
    await mount();
    assert.equal(find('form'), null);
    assert.ok(find('revoke'));
    await act(async () => find('revoke').click());
    await tick();
    assert.equal(calls.filter((x) => x.method.endsWith('.revoke')).length, 1);
  } finally {
    await unmount();
  }
});

test('committed exit warning refreshes once without repeating mutation or leaking server error', async () => {
  mock((method) => {
    if (method.endsWith('.revoke')) throw exitError();
    return page();
  });
  try {
    await mount();
    await act(async () => find('revoke').click());
    await tick();
    assert.equal(find('notice').dataset.variant, 'exitUnconfirmed');
    assert.equal(calls.filter((x) => x.method.endsWith('.revoke')).length, 1);
    assert.equal(calls.filter((x) => x.method.endsWith('.list')).length, 2);
    assert.ok(!document.body.textContent.includes('private server text'));
  } finally {
    await unmount();
  }
});

test('CAS conflict refreshes, while unknown outcome clears authority and requires manual reload', async () => {
  for (const code of ['CONFLICT', 'TIMEOUT', 'FORBIDDEN', 'MUTATION_OUTCOME_UNKNOWN']) {
    mock((method) => {
      if (method.endsWith('.revoke')) throw Object.assign(new Error('private'), { code });
      return page();
    });
    try {
      await mount();
      await act(async () => find('revoke').click());
      await tick();
      assert.equal(find('notice').dataset.variant, code === 'CONFLICT' ? 'conflict' : 'mutationFailed');
      assert.equal(calls.filter((x) => x.method.endsWith('.revoke')).length, 1);
      assert.equal(calls.filter((x) => x.method.endsWith('.list')).length, code === 'CONFLICT' ? 2 : 1);
      if (code !== 'CONFLICT') assert.equal(find('revoke'), null);
    } finally {
      await unmount();
    }
  }
});

test('empty catalog explains host registration instead of offering root creation', async () => {
  mock(() => ({ ...page(), resources: [] }));
  try {
    await mount();
    assert.ok(find('empty'));
    assert.equal(find('form'), null);
  } finally {
    await unmount();
  }
});

test('credential/disconnect invalidation aborts and drops late list data', async () => {
  for (const trigger of [() => notifyOrganizationCredentialChange(), () => stateListener('closed')]) {
    let finish, signal;
    mock((method, params, options) => {
      signal = options.signal;
      return new Promise((resolve) => {
        finish = resolve;
      });
    });
    try {
      await mount();
      await act(async () => trigger());
      assert.equal(signal.aborted, true);
      await act(async () => finish(page()));
      await tick();
      assert.equal(find('row'), null);
      assert.equal(find('notice').dataset.variant, 'invalidated');
    } finally {
      await unmount();
    }
  }
});

test('project switch and close discard old list and completed mutation replies', async () => {
  let finish;
  mock((method) =>
    method === 'project.resources.revoke'
      ? new Promise((resolve) => {
          finish = resolve;
        })
      : page(),
  );
  await mount();
  await act(async () => find('revoke').click());
  await act(async () => notifyOrganizationCredentialChange());
  await act(async () => finish(receipt));
  await tick();
  assert.equal(find('row'), null);
  assert.equal(calls.filter((x) => x.method.endsWith('.list')).length, 1);
  await unmount();
  let old;
  mock((method, params) =>
    params.project_id === 'p'
      ? new Promise((resolve) => {
          old = resolve;
        })
      : { ...page(), project_id: 'q', resources: [] },
  );
  await mount();
  await mount('q');
  await act(async () => old(page()));
  await tick();
  assert.equal(find('row'), null);
  assert.ok(find('empty'));
  await unmount();
  const before = calls.length;
  await tick();
  assert.equal(calls.length, before);
});

test('closing pending panel aborts list and prevents any late refill or follow-up', async () => {
  let finish, signal;
  mock((method, params, options) => {
    signal = options.signal;
    return new Promise((resolve) => {
      finish = resolve;
    });
  });
  await mount();
  await unmount();
  assert.equal(signal.aborted, true);
  const before = calls.length;
  await act(async () => finish(page()));
  await tick();
  assert.equal(find('row'), null);
  assert.equal(calls.length, before);
});

test('committed warning survives a failed refresh and unavailable grants do not imply permission', async () => {
  let count = 0;
  mock((method) => {
    if (method.endsWith('.revoke')) throw exitError();
    if (++count > 1) throw new Error('private refresh details');
    return page();
  });
  try {
    await mount();
    await act(async () => find('revoke').click());
    await tick();
    assert.equal(find('notice').dataset.variant, 'exitUnconfirmedRefreshFailed');
    assert.equal(find('row'), null);
    assert.equal(calls.filter((x) => x.method.endsWith('.revoke')).length, 1);
  } finally {
    await unmount();
  }
  const value = page();
  value.resources[0].can_grant = false;
  value.resources[0].grants[0].can_revoke = false;
  mock(() => value);
  try {
    await mount();
    assert.equal(find('form'), null);
    assert.equal(find('revoke'), null);
  } finally {
    await unmount();
  }
});

test('expiry exceeding parent authorization never submits, while null explicitly inherits', async () => {
  const value = page();
  value.resources[0].expires_at = Date.now() / 1000 + 3600;
  mock(() => value);
  try {
    await mount();
    await change('select', 'workspace');
    await change('target', 'bob');
    await act(async () => find('action').click());
    await change('expiry', '2099-01-01T12:00');
    await act(async () => find('submit').click());
    assert.equal(find('notice').dataset.variant, 'invalidInput');
    assert.equal(calls.filter((x) => x.method.endsWith('.grant')).length, 0);
  } finally {
    await unmount();
  }
});

test('default and explicitly disabled dialogs retain content without resource entry or RPC', async () => {
  for (const props of [{}, { resourcesEnabled: false }]) {
    mock();
    try {
      await act(async () =>
        root.render(
          React.createElement(ProjectContentDialog, {
            project: { project_id: 'p', name: 'p' },
            onClose() {},
            ...props,
          }),
        ),
      );
      await tick();
      assert.equal(document.querySelector('[data-testid="multi-session-project-tab-resources"]'), null);
      assert.equal(document.querySelector('[data-testid="multi-session-project-tabs"]'), null);
      assert.ok(document.querySelector('[data-testid="multi-session-project-content-save"]'));
      assert.equal(
        document.querySelector('[data-testid="multi-session-project-content-instructions"]').value,
        'Keep this draft',
      );
      assert.equal(calls.filter((call) => call.method.startsWith('project.resources.')).length, 0);
    } finally {
      await unmount();
    }
  }
});

test('removing organization visibility unmounts pending resources and cannot publish its late reply', async () => {
  let finish, signal;
  mock((method, params, options) => {
    signal = options.signal;
    return new Promise((resolve) => {
      finish = resolve;
    });
  });
  try {
    await mount();
    await act(async () =>
      root.render(
        React.createElement(ProjectContentDialog, {
          project: { project_id: 'p', name: 'p' },
          resourcesEnabled: false,
          onClose() {},
        }),
      ),
    );
    assert.equal(signal.aborted, true);
    await act(async () => finish(page()));
    await tick();
    assert.equal(find('row'), null);
    assert.equal(document.querySelector('[data-testid="multi-session-project-tab-resources"]'), null);
    const instructions = document.querySelector('[data-testid="multi-session-project-content-instructions"]');
    assert.equal(instructions.closest('.project-content-dialog__content').hidden, false);
    assert.equal(calls.filter((call) => call.method.startsWith('project.resources.')).length, 1);
  } finally {
    await unmount();
  }
});
