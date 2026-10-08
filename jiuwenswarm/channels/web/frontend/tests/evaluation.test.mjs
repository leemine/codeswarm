import test from 'node:test';
import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
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
  experiments = [{id:'independent',definition:{name:'Independent', model:'model', acceptance_policy:'independent-container-v1'},
    statistics:{passed:1,planned_trials:1,all_settled:true},trials:[{id:'trial',task_id:'task',repeat_index:0,attempts:[{
      id:'attempt',phase:'settled',body:{session_id:'code-original',outcome:'passed',exit_confirmed:true,
        acceptance_policy:'independent-container-v1',authority_sha256:'fixed-authority',authoritative_assertions:2,
        verification_environment:{image_id:'sha256:fixed-image',network:'none'},verifier_removed:true}}]}]}];
  await mount();
  assert.match(find('evaluation-attempt-status').textContent,/Independent acceptance passed/);
  assert.equal(find('evaluation-session-link').getAttribute('href'),'/chat/code-original');
  assert.match(find('evaluation-verifier-image').textContent,/sha256:fixed-image/);
  assert.match(find('evaluation-authority-digest').textContent,/fixed-authority/);
  assert.match(find('evaluation-verifier-cleanup').textContent,/Yes/);
  assert.ok(find('evaluation-start').disabled);
  await unmount();
});
