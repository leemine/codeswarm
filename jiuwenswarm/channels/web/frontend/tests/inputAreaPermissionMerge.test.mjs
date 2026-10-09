import assert from 'node:assert/strict';
import test, { after } from 'node:test';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { I18nextProvider } from 'react-i18next';
import { JSDOM } from 'jsdom';

// Reserved, non-resolving DOM origin only; fetch and WebSocket below reject all network access.
const dom = new JSDOM('<div id="root"></div>', { url: 'https://input-area.invalid', pretendToBeVisual: true });
const globals = {
  window: dom.window,
  document: dom.window.document,
  navigator: dom.window.navigator,
  localStorage: dom.window.localStorage,
  Node: dom.window.Node,
  HTMLElement: dom.window.HTMLElement,
  MutationObserver: dom.window.MutationObserver,
  CustomEvent: dom.window.CustomEvent,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  IS_REACT_ACT_ENVIRONMENT: true,
  ResizeObserver: class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
  fetch: () => {
    throw new Error('InputArea permission interactions must not perform HTTP requests');
  },
  WebSocket: class {
    constructor() {
      throw new Error('Unexpected WebSocket connection');
    }
  },
};
const descriptors = new Map(Object.keys(globals).map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
for (const [key, value] of Object.entries(globals)) {
  Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
}
after(() => {
  dom.window.close();
  for (const [key, descriptor] of descriptors) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor);
    else delete globalThis[key];
  }
});

const { InputArea } =
  await import('../node_modules/.cache/input-area-permission-merge/components/ChatPanel/InputArea.js');
const { useChatStore, useSessionStore, useWorkspaceStore, useGoalStore } =
  await import('../node_modules/.cache/input-area-permission-merge/stores/index.js');
const { default: i18n } = await import('../node_modules/.cache/input-area-permission-merge/i18n/index.js');

function byId(id, variant) {
  const selector = `[data-testid="${id}"]${variant === undefined ? '' : `[data-variant="${variant}"]`}`;
  const matches = document.querySelectorAll(selector);
  assert.equal(matches.length, 1, `${selector} must identify one actual InputArea element`);
  return matches[0];
}
const click = async (element) => act(async () => element.click());

for (const source of ['permission_interrupt', 'ask_user_interrupt']) {
  test(`${source}: live control retains Stop after the previous producer ends`, async () => {
    await mount({ draft: 'preserved follow-up' }, async ({ props, render, sessionId, submitted }) => {
      let cancelled = 0;
      props.onCancel = () => { cancelled += 1; };
      props.isProcessing = false;
      await act(async () => useChatStore.getState().enqueuePendingQuestion(sessionId, {
        request_id: 'control-continuation',
        source,
        questions: [{ header: 'Control', question: 'Continue?', card_id: 'pending-control', options: [{ label: 'Allow' }] }],
      }));
      await render();
      const stop = byId('chat-panel-input-send', 'stop');
      assert.equal(stop.disabled, false);
      await click(stop);
      assert.equal(cancelled, 1);
      assert.deepEqual(submitted, []);
      assert.equal(useChatStore.getState().getRuntime(sessionId).inputValue, 'preserved follow-up');
      await act(async () => useChatStore.getState().clearPendingQuestions(sessionId));
      assert.equal(byId('chat-panel-input-send', 'send').dataset.variant, 'send');
    });
  });
}

async function mount({ mode = 'agent', profile = 'default', language = 'en', organizationAuth = false, sessionId = 'input-permission-merge', draft = '' } = {}, run) {
  useSessionStore.getState().ensureRuntime(sessionId);
  useSessionStore.getState().setMode(sessionId, mode);
  useChatStore.getState().ensureRuntime(sessionId);
  useChatStore.getState().setActiveSessionId(sessionId);
  useChatStore.getState().setInputValue(sessionId, draft);
  const previousWorkspace = useWorkspaceStore.getState();
  useWorkspaceStore.setState({ workMode: 'work', projects: [], selectedProject: null });
  await i18n.changeLanguage(language);
  const saved = [];
  const switched = [];
  const submitted = [];
  const props = {
    organizationAuth,
    onSubmit(content, media) { submitted.push({ content, media }); },
    onInterrupt() {},
    onCancel() {},
    onPersistMedia: async () => ({}),
    onPersistDocuments: async () => ({}),
    onSwitchMode: (next) => {
      switched.push(next);
      useSessionStore.getState().setMode(sessionId, next);
    },
    isProcessing: false,
    permissionProfile: profile,
    onSavePermission: async (update) => {
      saved.push(update);
    },
  };
  document.getElementById('root').className = 'chat-panel-shell';
  const root = createRoot(document.getElementById('root'));
  const render = async () =>
    act(async () => root.render(createElement(I18nextProvider, { i18n }, createElement(InputArea, props))));
  try {
    await render();
    await run({ saved, switched, submitted, props, render, sessionId });
  } finally {
    await act(async () => root.unmount());
    useChatStore.getState().setActiveSessionId(null);
    useChatStore.getState().removeRuntime(sessionId);
    useGoalStore.getState().removeRuntime(sessionId);
    useSessionStore.getState().removeRuntime(sessionId);
    useWorkspaceStore.setState(previousWorkspace, true);
  }
}

for (const language of ['zh', 'en']) {
  test(`${language}: mode tooltip uses option DOMRect and already translated text`, async () => {
    await mount({ language }, async ({ switched }) => {
      await click(byId('chat-panel-mode-select-trigger'));
      const option = byId('chat-panel-mode-select-option', 'team');
      option.getBoundingClientRect = () => new dom.window.DOMRect(210, 320, 140, 46);
      await act(async () => option.dispatchEvent(new dom.window.MouseEvent('mouseover', { bubbles: true })));
      const tooltip = byId('chat-panel-mode-select-tooltip');
      assert.equal(tooltip.classList.contains('adaptive-tooltip'), true);
      assert.equal(tooltip.textContent, i18n.t('chat.config.mode.clusterDesc'));
      assert.notEqual(tooltip.textContent, 'chat.config.mode.clusterDesc');
      assert.equal(tooltip.style.position, 'fixed');
      assert.equal(tooltip.style.top, '326px');
      assert.equal(tooltip.style.left, '361px');
      await click(option);
      assert.deepEqual(switched, ['team']);
      assert.equal(document.querySelector('[data-testid="chat-panel-mode-select-tooltip"]'), null);
    });
  });
}

for (const mode of ['agent', 'auto_harness']) {
  test(`${mode}: persisted default profile is displayed without saving during render`, async () => {
    await mount({ mode, profile: 'default' }, async ({ saved, sessionId }) => {
      const effective = 'default';
      const trigger = byId('chat-panel-permission-selector-trigger');
      assert.equal(trigger.dataset.variant, effective);
      assert.match(trigger.textContent, new RegExp(i18n.t(`chat.config.permission.${effective}`)));
      await click(trigger);
      const options = [...document.querySelectorAll('[data-testid="chat-panel-permission-selector-option"]')];
      assert.deepEqual(
        options.map((option) => option.dataset.variant),
        ['default', 'full_access'],
      );
      assert.equal(byId('chat-panel-permission-selector-option', effective).getAttribute('aria-checked'), 'true');
      await click(byId('chat-panel-permission-selector-option', effective));
      assert.deepEqual(saved, []);
      await act(async () => useSessionStore.getState().setMode(sessionId, 'agent'));
      assert.equal(byId('chat-panel-permission-selector-trigger').dataset.variant, 'default');
      assert.deepEqual(saved, [], 'switching modes must not overwrite the persisted profile');
    });
  });
}

test('team hides the permission selector without overwriting the persisted profile', async () => {
  await mount({ mode: 'team', profile: 'default' }, async ({ saved, sessionId }) => {
    assert.equal(document.querySelector('[data-testid="chat-panel-permission-selector-trigger"]'), null);
    await act(async () => useSessionStore.getState().setMode(sessionId, 'agent'));
    assert.equal(byId('chat-panel-permission-selector-trigger').dataset.variant, 'default');
    assert.deepEqual(saved, []);
  });
});

test('default selection sends the profile contract and reflects the persisted prop', async () => {
  await mount({ profile: 'full_access' }, async ({ saved, props, render }) => {
    await click(byId('chat-panel-permission-selector-trigger'));
    await click(byId('chat-panel-permission-selector-option', 'default'));
    assert.deepEqual(saved, [{ permissions_profile: 'default' }]);
    assert.equal(document.querySelector('[data-testid="chat-panel-perm-warning-modal"]'), null);
    props.permissionProfile = 'default';
    await render();
    assert.equal(byId('chat-panel-permission-selector-trigger').dataset.variant, 'default');
  });
});

for (const action of ['cancel', 'confirm']) {
  test(`full access ${action} preserves the warning flow`, async () => {
    await mount({}, async ({ saved }) => {
      await click(byId('chat-panel-permission-selector-trigger'));
      await click(byId('chat-panel-permission-selector-option', 'full_access'));
      assert.deepEqual(saved, []);
      assert.equal(
        byId('chat-panel-perm-warning-title').textContent,
        i18n.t('chat.config.permission.fullAccessWarning.title'),
      );
      await click(byId(`chat-panel-perm-warning-${action}`));
      assert.deepEqual(saved, action === 'confirm' ? [{ permissions_profile: 'full_access' }] : []);
      assert.equal(document.querySelector('[data-testid="chat-panel-perm-warning-modal"]'), null);
    });
  });
}


test('organization keeps Team visible but disabled without changing the selected mode', async () => {
  await mount({ organizationAuth: true }, async ({ switched, sessionId }) => {
    await click(byId('chat-panel-mode-select-trigger'));
    const option = byId('chat-panel-mode-select-option', 'team');
    assert.equal(option.disabled, true);
    assert.equal(option.title, i18n.t('organizationRelease.teamUnavailable'));
    await click(option);
    assert.deepEqual(switched, []);
    assert.equal(useSessionStore.getState().getRuntime(sessionId).mode, 'agent');
  });
});

async function submitText(value) {
  const input = byId('chat-panel-input');
  await act(async () => {
    input.innerHTML = value;
    input.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  });
  await act(async () => input.dispatchEvent(new dom.window.KeyboardEvent('keydown', {
    key: 'Enter', code: 'Enter', bubbles: true,
  })));
}

test('organization hides Goal entry and blocks stale armed draft rather than sending ordinary chat', async () => {
  await mount({ organizationAuth: true }, async ({ props, render, sessionId }) => {
    const calls = [];
    props.onSetGoal = (...args) => calls.push(['goal', ...args]);
    props.onSubmit = (...args) => calls.push(['chat', ...args]);
    await render();
    await click(byId('chat-panel-input-attach-trigger'));
    assert.equal(document.querySelector('[data-testid="chat-panel-input-attach-menu-goal"]'), null);
    await act(async () => useGoalStore.getState().setArmed(sessionId, true));
    await submitText('preserved goal objective');
    assert.deepEqual(calls, []);
    assert.equal(useGoalStore.getState().getRuntime(sessionId).armed, true);
    assert.ok(document.body.textContent.includes(i18n.t('organizationRelease.goalUnavailable')));
  });
});

test('organization slash Goal mutation is rejected but get still uses its original handler', async () => {
  await mount({ organizationAuth: true }, async ({ props, render }) => {
    const calls = [];
    props.onSetGoal = () => calls.push('set');
    props.onRefreshGoal = () => calls.push('get');
    await render();
    await submitText('/goal set hidden mutation');
    assert.deepEqual(calls, []);
    await submitText('/goal');
    assert.deepEqual(calls, ['get']);
  });
});

test('nonorganization Goal attachment and armed send keep existing behavior', async () => {
  await mount({}, async ({ props, render, sessionId }) => {
    const calls = [];
    props.onSetGoal = (...args) => calls.push(args);
    await render();
    await click(byId('chat-panel-input-attach-trigger'));
    assert.ok(byId('chat-panel-input-attach-menu-goal'));
    await act(async () => useGoalStore.getState().setArmed(sessionId, true));
    await submitText('ordinary legacy goal');
    assert.deepEqual(calls, [[sessionId, 'ordinary legacy goal']]);
  });
});

for (const organizationAuth of [true, false]) {
  test(`new draft sends without project: organization=${organizationAuth}`, async () => {
    const draft = 'Hi without a project';
    await mount({ organizationAuth, sessionId: 'new', draft }, async ({ submitted, sessionId }) => {
      const input = byId('chat-panel-input');
      assert.equal(input.textContent, draft);
      await click(byId('chat-panel-input-send'));
      assert.equal(submitted.length, 1);
      assert.equal(submitted[0].content, draft);
      assert.equal(input.textContent, '');
    });
  });
}


test('existing session displays its Provider without a switch or new-session dialog', async () => {
  await mount({}, async () => {
    const binding = byId('chat-panel-execution-binding');
    assert.equal(binding.tagName, 'SPAN');
    assert.equal(document.querySelector('[data-testid="chat-panel-execution-trigger"]'), null);
    await click(binding);
    assert.equal(document.querySelector('[data-testid="chat-panel-execution-menu"]'), null);
    assert.equal(document.querySelector('[data-testid="chat-panel-execution-new-dialog"]'), null);
  });
});
test('new conversation retains its Provider picker', async () => {
  await mount({ sessionId: 'new' }, async () => {
    assert.equal(byId('chat-panel-execution-trigger').tagName, 'BUTTON');
    assert.equal(document.querySelector('[data-testid="chat-panel-execution-binding"]'), null);
  });
});
