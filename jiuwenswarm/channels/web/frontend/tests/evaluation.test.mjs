import test from 'node:test';
import assert from 'node:assert/strict';
import React, { act } from 'react';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';
import { webcrypto } from 'node:crypto';
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/' });
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  sessionStorage: dom.window.sessionStorage,
  CustomEvent: dom.window.CustomEvent,
  IS_REACT_ACT_ENVIRONMENT: true,
});
if (!globalThis.crypto) globalThis.crypto = webcrypto;
await i18next
  .use(initReactI18next)
  .init({ lng: 'en', showSupportNotice: false, resources: { en: { translation: {} } } });
const { default: EvaluationApp } =
  await import('../node_modules/.cache/evaluation/extensions/evaluation/frontend/EvaluationApp.js');
const { webClient } = await import('../node_modules/.cache/evaluation/channels/web/frontend/src/services/webClient.js');
const { createRoot } = await import('react-dom/client');
const root = createRoot(document.getElementById('root'));
const find = (id) => document.querySelector(`[data-testid="${id}"]`);
const calls = [];
let experiments = [];
webClient.request = async (method, params) => {
  calls.push({ method, params });
  if (method === 'evaluation.options') return { models: [], profiles: [], execution_available: false };
  if (method === 'evaluation.catalog') return { tasks: [], drafts: [], datasets: [] };
  if (method === 'evaluation.experiment.list') return { experiments };
  if (method === 'evaluation.evidence') throw new Error('FORBIDDEN');
  throw new Error(`unexpected ${method}`);
};
const mount = async () => {
  await act(async () => {
    root.render(React.createElement(EvaluationApp));
  });
};
const unmount = async () => {
  await act(async () => root.render(null));
};

test.afterEach(unmount);

test('empty RPC list stays usable and unavailable execution cannot submit', async () => {
  await mount();
  assert.ok(find('evaluation-app'));
  await act(async () => find('evaluation-new-experiment').click());
  assert.ok(find('evaluation-wizard-next').disabled);
  assert.ok(find('evaluation-create').disabled);
  assert.ok(find('evaluation-execution-unavailable'));
  assert.equal(document.querySelectorAll('[data-testid="evaluation-run"]').length, 0);
  assert.ok(calls.every((call) => !call.method.includes('.start')));
  await unmount();
});
test('refresh remount reads original attempt; evidence export reauthorizes and surfaces denial', async () => {
  experiments = [
    {
      id: 'exp',
      definition: { name: 'Original', model: 'model', acceptance_policy: 'shared-environment-v1' },
      statistics: { passed: 0, planned_trials: 1, all_settled: false },
      trials: [
        {
          id: 'trial',
          task_id: 'task',
          repeat_index: 0,
          attempts: [
            {
              id: 'attempt',
              phase: 'observing',
              body: { session_id: 'original-session', runtime: { state: 'waiting_for_control' } },
            },
          ],
        },
      ],
    },
  ];
  await mount();
  assert.match(find('evaluation-attempt-status').textContent, /Waiting for control/);
  assert.equal(find('evaluation-session-link').getAttribute('href'), '/chat/original-session');
  await act(async () => find('evaluation-export').click());
  assert.match(find('evaluation-experiment-error').textContent, /FORBIDDEN/);
  assert.equal(calls.filter((call) => call.method === 'evaluation.evidence').length, 1);
  await unmount();
  await mount();
  assert.equal(find('evaluation-attempt').getAttribute('data-variant'), 'attempt');
  assert.equal(calls.filter((call) => /experiment\.(create|start)/.test(call.method)).length, 0);
  await unmount();
});

test('independent acceptance shows authoritative evidence and preserves original Code link', async () => {
  experiments = [
    {
      id: 'independent',
      definition: { name: 'Independent', model: 'model', acceptance_policy: 'independent-container-v1' },
      statistics: { passed: 1, planned_trials: 1, all_settled: true, first_attempt_outcomes: { passed: 1 } },
      trials: [
        {
          id: 'trial',
          task_id: 'task',
          repeat_index: 0,
          attempts: [
            {
              id: 'attempt',
              phase: 'settled',
              body: {
                session_id: 'code-original',
                outcome: 'passed',
                exit_confirmed: true,
                acceptance_policy: 'independent-container-v1',
                authority_sha256: 'fixed-authority',
                authoritative_assertions: 2,
                verification_environment: { image_id: 'sha256:fixed-image', network: 'none' },
                verifier_removed: true,
              },
            },
          ],
        },
      ],
    },
  ];
  await mount();
  assert.match(find('evaluation-attempt-outcome').textContent, /Independent acceptance passed/);
  assert.match(find('evaluation-outcome-count').textContent, /Independent acceptance passed: 1/);
  assert.equal(find('evaluation-session-link').getAttribute('href'), '/chat/code-original');
  assert.match(find('evaluation-verifier-image').textContent, /sha256:fixed-image/);
  assert.match(find('evaluation-authority-digest').textContent, /fixed-authority/);
  assert.match(find('evaluation-verifier-cleanup').textContent, /Yes/);
  assert.ok(find('evaluation-start').disabled);
  await unmount();
});

const { ExperimentsContainer } = await import('../node_modules/.cache/evaluation-container/ExperimentsContainer.mjs');
const plugin = {
  plugin_id: 'evaluation-experiments',
  nav_key: 'app:evaluation-experiments',
  title: 'Evaluation',
  nav_group: 'experiments',
};
const host = async (rsiEnabled, plugins, legacyNav = 'experiments') => {
  await act(async () => root.render(React.createElement(ExperimentsContainer, { rsiEnabled, plugins, legacyNav })));
};
test('independent experiment feature combinations and old navigation resolve without duplicate outlets', async () => {
  sessionStorage.clear();
  await host(false, [plugin]);
  assert.ok(find('plugin-outlet'));
  assert.equal(find('app-experiments-rsi-tab'), null);
  await host(true, [plugin]);
  await act(async () => find('app-experiments-rsi-tab').click());
  assert.ok(find('original-rsi-outlet'));
  assert.equal(find('plugin-outlet'), null);
  await host(true, [plugin], plugin.nav_key);
  assert.ok(find('plugin-outlet'));
  assert.equal(find('original-rsi-outlet'), null);
  await host(true, []);
  assert.ok(find('original-rsi-outlet'));
  await host(false, []);
  assert.equal(find('original-rsi-outlet'), null);
  assert.equal(find('plugin-outlet'), null);
});
test('outer tab remount restores the original choice without starting an experiment', async () => {
  sessionStorage.clear();
  await host(true, [plugin]);
  await act(async () => find('app-experiments-rsi-tab').click());
  await unmount();
  await host(true, [plugin]);
  assert.ok(find('original-rsi-outlet'));
  assert.equal(calls.filter((call) => /experiment\.(create|start)/.test(call.method)).length, 0);
});

test('three-step creation freezes a single configuration and never starts execution implicitly', async () => {
  sessionStorage.clear();
  const originalRequest = webClient.request;
  let created;
  webClient.request = async (method, params) => {
    calls.push({ method, params });
    if (method === 'evaluation.options')
      return {
        models: [{ selection_key: 'model', display_name: 'Model' }],
        profiles: [{ id: 'native', revision: 'v1' }],
        execution_available: true,
        acceptance_policies: ['shared-environment-v1', 'independent-container-v1'],
      };
    if (method === 'evaluation.catalog')
      return {
        tasks: [
          { id: 'task', revision: 1, value: { name: 'Task', instruction: 'Do it', acceptance: { kind: 'manual' } } },
        ],
        drafts: [],
        datasets: [],
      };
    if (method === 'evaluation.experiment.list') return { experiments: created ? [created] : [] };
    if (method === 'evaluation.experiment.create') {
      created = { id: 'created', definition: params.experiment, trials: [] };
      return created;
    }
    throw new Error(`unexpected ${method}`);
  };
  try {
    await mount();
    await act(async () => find('evaluation-new-experiment').click());
    assert.ok(find('evaluation-wizard-next').disabled);
    await act(async () => find('evaluation-wizard-task-select').click());
    await act(async () => find('evaluation-wizard-next').click());
    assert.equal(find('evaluation-wizard-configuration').hidden, false);
    assert.ok(find('evaluation-wizard-next').disabled);
    await act(async () => {
      const input = find('evaluation-experiment-name');
      Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(input, 'Frozen UI');
      input.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
      input.dispatchEvent(new dom.window.FocusEvent('focusout', { bubbles: true }));
    });
    await act(async () => find('evaluation-wizard-next').click());
    assert.equal(find('evaluation-wizard-confirmation').hidden, false);
    assert.match(find('evaluation-confirm-frozen').textContent, /Creating the experiment freezes this configuration/);
    assert.ok(find('evaluation-create').disabled);
    await act(async () => find('evaluation-acknowledge').click());
    await act(async () => find('evaluation-create').click());
    assert.equal(Boolean(find('evaluation-wizard')), false);
    assert.deepEqual(created.definition.tasks, [{ task_id: 'task', revision: 1 }]);
    assert.equal(created.definition.execution_profile_id, 'native');
    assert.equal(created.definition.name, 'Frozen UI');
    assert.equal(calls.filter((call) => call.method === 'evaluation.experiment.create').length, 1);
    assert.equal(calls.filter((call) => call.method === 'evaluation.experiment.start').length, 0);
  } finally {
    webClient.request = originalRequest;
  }
});

for (const scenario of ['list-failure', 'obsolete-poll']) {
  test(`a confirmed freeze survives ${scenario} without offering a duplicate submission`, async () => {
    sessionStorage.clear();
    const originalRequest = webClient.request;
    let created;
    let creates = 0;
    let listReads = 0;
    const oldPoll = deferred();
    webClient.request = async (method, params) => {
      if (method === 'evaluation.options') return {
        models: [{ selection_key: 'model', display_name: 'Model' }],
        profiles: [{ id: 'native', revision: 'v1' }], execution_available: true,
      };
      if (method === 'evaluation.catalog') return {
        tasks: [{ id: 'task', revision: 1, value: { name: 'Task', instruction: 'Do it' } }],
        drafts: [], datasets: [],
      };
      if (method === 'evaluation.experiment.list') {
        listReads += 1;
        if (scenario === 'obsolete-poll' && listReads === 1) return oldPoll.promise;
        if (created && scenario === 'list-failure') throw new Error('LIST_CONNECTION_LOST');
        if (created) return { experiments: [created] };
        return { experiments: [] };
      }
      if (method === 'evaluation.experiment.create') {
        creates += 1;
        created = { id: 'confirmed', definition: params.experiment, trials: [] };
        return created;
      }
      throw new Error(`unexpected ${method}`);
    };
    try {
      await mount();
      await act(async () => find('evaluation-new-experiment').click());
      await act(async () => find('evaluation-wizard-task-select').click());
      await act(async () => find('evaluation-wizard-next').click());
      await act(async () => {
        const input = find('evaluation-experiment-name');
        Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(input, 'Confirmed freeze');
        input.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
        input.dispatchEvent(new dom.window.FocusEvent('focusout', { bubbles: true }));
      });
      await act(async () => find('evaluation-wizard-next').click());
      await act(async () => find('evaluation-acknowledge').click());
      await act(async () => find('evaluation-create').click());
      if (scenario === 'list-failure') {
        assert.match(find('evaluation-experiment-error').textContent, /LIST_CONNECTION_LOST/);
      } else {
        await act(async () => oldPoll.resolve({ experiments: [] }));
      }
      assert.equal(Boolean(find('evaluation-wizard')), false);
      assert.equal(find('evaluation-run-name').textContent, 'Confirmed freeze');
      assert.equal(creates, 1);
      await act(async () => find('evaluation-run-refresh').click());
      assert.equal(creates, 1);
      assert.equal(find('evaluation-run-name').textContent, 'Confirmed freeze');
    } finally {
      webClient.request = originalRequest;
    }
  });
}

const { useApplicationPlugins } = await import('../node_modules/.cache/evaluation-container/useApplicationPlugins.mjs');
let discoveryState;
function DiscoveryProbe({ connected }) {
  discoveryState = useApplicationPlugins(connected);
  return React.createElement('div', { 'data-testid': 'discovery-probe' }, JSON.stringify(discoveryState));
}
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};
for (const scenario of ['latest-refresh', 'disconnect']) {
  test(`plugin discovery ignores obsolete responses after ${scenario}`, async () => {
    const originalFetch = globalThis.fetch;
    const pending = [];
    globalThis.fetch = (_url, options) => {
      const response = deferred();
      pending.push({ ...response, signal: options.signal });
      return response.promise;
    };
    const respond = (index, enabled) => act(async () => pending[index].resolve({
      ok: true,
      json: async () => ({ api_version: 1, plugins: [{ ...plugin, id: 'evaluation', enabled, render_mode: 'bundled', position: 80 }] }),
    }));
    try {
      await act(async () => root.render(React.createElement(DiscoveryProbe, { connected: true })));
      await act(async () => window.dispatchEvent(new dom.window.Event('jiuwen:application-plugins-refresh')));
      assert.equal(pending.length, 2);
      if (scenario === 'latest-refresh') {
        await respond(1, false);
        await respond(0, true);
        assert.equal(discoveryState.plugins[0].enabled, false);
      } else {
        await act(async () => root.render(React.createElement(DiscoveryProbe, { connected: false })));
        await respond(1, true);
        assert.deepEqual(discoveryState.plugins, []);
        assert.equal(discoveryState.loaded, false);
      }
      assert.equal(discoveryState.loading, false);
    } finally {
      await unmount();
      globalThis.fetch = originalFetch;
    }
  });
}
