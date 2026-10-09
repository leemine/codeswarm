import test, { beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/' });
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  localStorage: dom.window.localStorage,
  sessionStorage: dom.window.sessionStorage,
  CustomEvent: dom.window.CustomEvent,
  IS_REACT_ACT_ENVIRONMENT: true,
});
const base = '../node_modules/.cache/codex-governed-availability/';
const { ExternalCliAgentsSection } = await import(base + 'components/ExternalCliAgentsSection.js');
const { ExternalCliSettingsItem } = await import(
  base + 'features/settings/modules/experimental/ExperimentalSettings.js'
);
const { SettingsServicesProvider } = await import(base + 'features/settings/services/SettingsServicesProvider.js');
const { SettingsSourceProvider } = await import(base + 'features/settings/services/SettingsSourceProvider.js');
const en = JSON.parse(readFileSync(new URL('../src/i18n/locales/en.json', import.meta.url), 'utf8'));
await i18next
  .use(initReactI18next)
  .init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
const values = {
  external_cli_agent_codex_enabled: 'true',
  external_cli_agent_codex_use_builtin: 'false',
  external_cli_agent_codex_cli_path: '/synthetic/codex',
  external_cli_agent_claude_enabled: 'true',
  external_cli_agent_claude_use_builtin: 'true',
  external_cli_agent_claude_cli_path: '',
};
const select = (name, agent = 'codex') =>
  document.querySelector(`[data-testid="settings-panel-external-cli-agent-${name}"][data-variant="${agent}"]`);
const tick = (ms = 0) =>
  act(async () => {
    await new Promise((resolve) => setTimeout(resolve, ms));
  });
let root;
beforeEach(() => {
  root = createRoot(document.getElementById('root'));
});
afterEach(async () => {
  await act(async () => root.unmount());
});
async function section(extra = {}) {
  await act(async () =>
    root.render(
      React.createElement(ExternalCliAgentsSection, {
        draftValues: values,
        onChange() {},
        t: i18next.t.bind(i18next),
        ...extra,
      }),
    ),
  );
}

test('organization mode shows the native boundary blocker and preserves stored choice', async () => {
  const changes = [],
    detects = [];
  await section({
    organizationAuth: true,
    onChange: (...args) => changes.push(args),
    onDetect: async (agent) => {
      detects.push(agent);
      return { cli_agent: agent, status: 'ok' };
    },
    initialResults: { codex: { cli_agent: 'codex', status: 'ok', version: 'synthetic', path: '/synthetic/old' } },
  });
  assert.equal(select('toggle').disabled, true);
  assert.equal(select('toggle').getAttribute('aria-checked'), 'true');
  assert.match(select('unavailable').textContent, /0\.144\.4/);
  assert.match(select('unavailable').textContent, /native tool/);
  assert.equal(select('toggle', 'claude').disabled, false);
  assert.equal(select('detect-btn'), null);
  assert.equal(select('status'), null);
  await act(async () => select('toggle').click());
  await tick(450);
  assert.deepEqual(changes, []);
  assert.deepEqual(detects, ['claude']);
});

test('legacy omitted mode still allows Codex toggle and detection', async () => {
  const changes = [],
    detects = [];
  await section({
    onChange: (...args) => changes.push(args),
    onDetect: async (agent) => {
      detects.push(agent);
      return { cli_agent: agent, status: 'ok' };
    },
  });
  assert.equal(select('unavailable'), null);
  assert.equal(select('toggle').disabled, false);
  await act(async () => select('toggle').click());
  await tick(450);
  assert.deepEqual(changes, [['external_cli_agent_codex_enabled', 'false']]);
  assert.deepEqual(detects.sort(), ['claude', 'codex']);
});

test('changing organization scope updates controls without changing stored configuration', async () => {
  const changes = [];
  await section({ organizationAuth: true, onChange: (...args) => changes.push(args) });
  assert.equal(select('toggle').disabled, true);
  await section({ organizationAuth: false, onChange: (...args) => changes.push(args) });
  assert.equal(select('toggle').disabled, false);
  assert.equal(select('unavailable'), null);
  assert.equal(select('toggle').getAttribute('aria-checked'), 'true');
  assert.deepEqual(changes, []);
});

async function settings(organizationAuth, pending) {
  const calls = [],
    pendingChanges = [];
  const request = async (method, params) => {
    calls.push([method, params]);
    return method === 'config.get' ? values : {};
  };
  await act(async () =>
    root.render(
      React.createElement(
        SettingsServicesProvider,
        {
          organizationAuth,
          isConnected: true,
          connectionState: 'connected',
          request,
          externalCliPendingChoices: pending,
          externalCliInstallStatuses: { codex: { status: 'succeeded' } },
          onExternalCliPendingChoicesChange: (value) => pendingChanges.push(value),
        },
        React.createElement(
          SettingsSourceProvider,
          { source: 'config' },
          React.createElement(ExternalCliSettingsItem, { disabled: false }),
        ),
      ),
    ),
  );
  await tick();
  return { calls, pendingChanges };
}

test('real Settings services/source path does not replay or save organization Codex pending enable', async () => {
  const pending = { codex: { enabled: 'true', useBuiltin: 'true', cliPath: '' } };
  const { calls, pendingChanges } = await settings(true, pending);
  assert.equal(select('toggle').disabled, true);
  assert.ok(select('unavailable'));
  assert.deepEqual(pendingChanges, []);
  const apply = document.querySelector('[data-testid="settings-experimental-cli-save-btn"]');
  assert.ok(apply);
  await act(async () => apply.click());
  await tick();
  assert.equal(calls.filter(([method]) => method === 'config.save_all').length, 0);
  assert.deepEqual(pending.codex, { enabled: 'true', useBuiltin: 'true', cliPath: '' });
  await act(async () => select('toggle', 'claude').click());
  await act(async () => document.querySelector('[data-testid="settings-experimental-cli-save-btn"]').click());
  await tick();
  const saved = calls.filter(([method]) => method === 'config.save_all');
  assert.equal(saved.length, 1);
  assert.ok(Object.keys(saved[0][1].config).every((key) => !key.includes('codex')));
});

test('legacy Settings still replays a successful deferred Codex choice', async () => {
  const { calls } = await settings(false, { codex: { enabled: 'true', useBuiltin: 'true', cliPath: '' } });
  assert.equal(calls.filter(([method]) => method === 'config.save_all').length, 1);
  assert.equal(select('unavailable'), null);
});

test('disabled Claude never auto-detects on mount or path edit; enabled Claude still detects', async () => {
  const detects = [];
  const onDetect = async (agent) => {
    detects.push(agent);
    return { cli_agent: agent, status: 'ok' };
  };
  const disabled = { ...values, external_cli_agent_claude_enabled: 'false' };
  await section({ organizationAuth: true, draftValues: disabled, onDetect });
  await tick(450);
  await section({ organizationAuth: true, draftValues: {
    ...disabled, external_cli_agent_claude_cli_path: '/synthetic/changed',
  }, onDetect });
  await tick(450);
  assert.deepEqual(detects, []);
  await section({ organizationAuth: true, draftValues: values, onDetect });
  await tick(450);
  assert.deepEqual(detects, ['claude']);
});
