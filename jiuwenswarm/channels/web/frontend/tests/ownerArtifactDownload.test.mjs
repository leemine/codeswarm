import assert from 'node:assert/strict';
import test, { beforeEach, afterEach } from 'node:test';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';
import { readFileSync } from 'node:fs';
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/' });
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  localStorage: dom.window.localStorage,
  sessionStorage: dom.window.sessionStorage,
  CustomEvent: dom.window.CustomEvent,
  IS_REACT_ACT_ENVIRONMENT: true,
});
const base = '../node_modules/.cache/owner-artifact-download/';
const { OwnerDownloadScope, ownerDownloadUrl } = await import(base + 'components/ArtifactsPanel/ownerDownload.js');
const { ArtifactOwnerContext } = await import(base + 'components/ArtifactsPanel/ArtifactOwnerContext.js');
const { ArtifactList } = await import(base + 'components/ArtifactsPanel/index.js');
const { useChatStore, useSessionStore } = await import(base + 'stores/index.js');
const { webClient } = await import(base + 'services/webClient.js');
const en = JSON.parse(readFileSync(new URL('../src/i18n/locales/en.json', import.meta.url), 'utf8'));
await i18next.use(initReactI18next).init({ lng: 'en', resources: { en: { translation: en } } });
const artifact = {
  id: 'one',
  name: 'report.txt',
  source: 'message',
  downloadUrl: '/file-api/download?token=opaque',
  downloadToken: 'opaque',
};
let calls, saved, root, originalFetch, originalCreate, originalRevoke, originalClick;
beforeEach(() => {
  calls = [];
  saved = [];
  originalFetch = globalThis.fetch;
  originalCreate = URL.createObjectURL;
  originalRevoke = URL.revokeObjectURL;
  originalClick = dom.window.HTMLAnchorElement.prototype.click;
  URL.createObjectURL = (blob) => {
    saved.push(blob);
    return 'blob:synthetic';
  };
  URL.revokeObjectURL = () => {};
  dom.window.HTMLAnchorElement.prototype.click = () => {};
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    return { ok: true, redirected: false, blob: async () => new Blob(['fixture']) };
  };
  webClient.updateState('ready');
  useChatStore.setState({
    activeSessionId: 'owner',
    runtimes: {
      other: { messages: [] },
      owner: {
        messages: [
          {
            id: 'm',
            role: 'assistant',
            timestamp: '',
            fileItems: [{ name: 'report.txt', download_url: artifact.downloadUrl }],
          },
        ],
      },
    },
  });
});
afterEach(async () => {
  if (root) {
    await act(async () => root.unmount());
    root = null;
  }
  delete window.pywebview;
  globalThis.fetch = originalFetch;
  URL.createObjectURL = originalCreate;
  URL.revokeObjectURL = originalRevoke;
  dom.window.HTMLAnchorElement.prototype.click = originalClick;
});
test('strict token routing fixes original session and rejects path/absolute/conflicting/duplicate fields', () => {
  assert.equal(ownerDownloadUrl(artifact, 'owner'), '/file-api/download?token=opaque&session_id=owner');
  assert.equal(ownerDownloadUrl({ downloadToken: 'a&b' }, 'owner'), '/file-api/download?token=a%26b&session_id=owner');
  for (const url of [
    'https://localhost/file-api/download?token=opaque',
    '//localhost/file-api/download?token=opaque',
    '/file-api/raw-file?path=a',
    '/file-api/download?token=a&token=b',
    '/file-api/download?token=opaque&path=a',
    '/file-api/download?token=opaque&session_id=other',
    '/file-api/download?token=opaque#x',
  ])
    assert.equal(ownerDownloadUrl({ downloadUrl: url }, 'owner'), null);
  assert.equal(ownerDownloadUrl({ path: '/tmp/fixture' }, 'owner'), null);
  assert.equal(ownerDownloadUrl(artifact, 'new'), null);
  assert.equal(ownerDownloadUrl({ ...artifact, downloadToken: 'different' }, 'owner'), null);
});
test('browser fetch is authenticated same-origin without redirects, then saves a blob', async () => {
  assert.equal(await new OwnerDownloadScope().download(artifact, 'owner', () => true), 'saved');
  assert.equal(calls[0].url, '/file-api/download?token=opaque&session_id=owner');
  assert.equal(calls[0].options.credentials, 'same-origin');
  assert.equal(calls[0].options.mode, 'same-origin');
  assert.equal(calls[0].options.redirect, 'error');
  assert.equal(calls[0].options.cache, 'no-store');
  assert.equal(saved.length, 1);
});
test('generation rejects a late body even when fetch ignores abort', async () => {
  let finish;
  const scope = new OwnerDownloadScope();
  globalThis.fetch = async () => ({
    ok: true,
    blob: () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  });
  const pending = scope.download(artifact, 'owner', () => true);
  await Promise.resolve();
  scope.invalidate();
  finish(new Blob(['old']));
  assert.equal(await pending, 'cancelled');
  assert.equal(saved.length, 0);
});
test('server rejection does not parse or save an error body', async () => {
  globalThis.fetch = async () => ({
    ok: false,
    blob() {
      throw Error('must not read');
    },
  });
  assert.equal(await new OwnerDownloadScope().download(artifact, 'owner', () => true), 'failed');
  assert.equal(saved.length, 0);
});
test('desktop begin awaiting identity change aborts the original transaction and never uses URL downloader', async () => {
  const scope = new OwnerDownloadScope();
  let beginResolve;
  const actions = [];
  window.pywebview = {
    api: {
      download_file() {
        throw Error('legacy URL downloader');
      },
      begin_blob_save() {
        actions.push('begin');
        return new Promise((resolve) => {
          beginResolve = resolve;
        });
      },
      append_blob_save() {
        actions.push('append');
        return true;
      },
      finish_blob_save() {
        actions.push('finish');
        return { ok: true };
      },
      abort_blob_save(id) {
        actions.push(['abort', id]);
        return true;
      },
    },
  };
  const pending = scope.download(artifact, 'owner', () => true);
  while (!beginResolve) await Promise.resolve();
  scope.invalidate();
  beginResolve({ ok: true, transfer_id: 'original' });
  assert.equal(await pending, 'cancelled');
  assert.deepEqual(actions, ['begin', ['abort', 'original']]);
});
async function render() {
  root = createRoot(document.getElementById('root'));
  await act(async () =>
    root.render(
      React.createElement(
        ArtifactOwnerContext.Provider,
        { value: { organizationAuth: true, sessionId: 'owner' } },
        React.createElement(ArtifactList),
      ),
    ),
  );
}
for (const transition of ['session', 'connection', 'identity'])
  test(`actual artifact button aborts on ${transition} transition`, async () => {
    let resolveFetch, signal;
    globalThis.fetch = (_url, options) => {
      signal = options.signal;
      return new Promise((resolve) => {
        resolveFetch = resolve;
      });
    };
    await render();
    const button = document.querySelector('[data-testid="artifact-list-item-download"]');
    assert.ok(button);
    await act(async () => button.click());
    await act(async () => {
      if (transition === 'session') useChatStore.setState({ activeSessionId: 'other' });
      if (transition === 'connection') webClient.updateState('closed');
      if (transition === 'identity') window.dispatchEvent(new window.Event('jiuwen:organization-credentials-changed'));
    });
    assert.equal(signal.aborted, true);
    await act(async () => resolveFetch({ ok: true, blob: async () => new Blob(['old']) }));
    assert.equal(saved.length, 0);
  });
test('actual owner button succeeds and uses only a blob save', async () => {
  await render();
  await act(async () => document.querySelector('[data-testid="artifact-list-item-download"]').click());
  assert.equal(calls.length, 1);
  assert.equal(saved.length, 1);
});
test('path-only artifact stays visible but its download is unavailable', async () => {
  useChatStore.setState({
    runtimes: { owner: { messages: [{ id: 'p', fileItems: [{ name: 'private.txt', path: '/tmp/private.txt' }] }] } },
  });
  await render();
  const button = document.querySelector('[data-testid="artifact-list-item-download"]');
  assert.equal(button.disabled, true);
  assert.match(button.title, /No authorized Workspace/);
  await act(async () => button.click());
  assert.equal(calls.length, 0);
});
test('organization selection never opens the desktop file browser and preview makes no request', async () => {
  const { ArtifactExpandedPanel } = await import(base + 'components/ArtifactsPanel/index.js');
  let selected;
  root = createRoot(document.getElementById('root'));
  const renderPanel = (id) =>
    root.render(
      React.createElement(
        ArtifactOwnerContext.Provider,
        { value: { organizationAuth: true, sessionId: 'owner' } },
        React.createElement(ArtifactExpandedPanel, {
          selectedArtifactId: id,
          onSelectArtifact: (value) => {
            selected = value;
          },
        }),
      ),
    );
  await act(async () => renderPanel());
  await act(async () => document.querySelector('[data-testid="artifact-list-item"]').click());
  assert.ok(selected);
  await act(async () => renderPanel(selected));
  assert.ok(document.querySelector('[data-testid="artifact-owner-preview-notice"]'));
  assert.equal(calls.length, 0);
});
test('desktop ordinary save uses begin/append/finish and no URL downloader', async () => {
  const originalReader = globalThis.FileReader;
  const actions = [];
  globalThis.FileReader = class {
    readAsDataURL() {
      this.result = 'data:text/plain;base64,QQ==';
      queueMicrotask(() => this.onload());
    }
  };
  window.pywebview = {
    api: {
      download_file() {
        throw Error('legacy path');
      },
      begin_blob_save() {
        actions.push('begin');
        return { ok: true, transfer_id: 't' };
      },
      append_blob_save() {
        actions.push('append');
        return true;
      },
      finish_blob_save() {
        actions.push('finish');
        return { ok: true };
      },
      abort_blob_save() {
        actions.push('abort');
        return true;
      },
    },
  };
  try {
    assert.equal(await new OwnerDownloadScope().download(artifact, 'owner', () => true), 'saved');
    assert.deepEqual(actions, ['begin', 'append', 'finish']);
  } finally {
    globalThis.FileReader = originalReader;
  }
});
test('desktop append invalidation prevents commit and aborts only original transfer', async () => {
  const originalReader = globalThis.FileReader;
  const actions = [];
  const scope = new OwnerDownloadScope();
  globalThis.FileReader = class {
    readAsDataURL() {
      this.result = 'data:text/plain;base64,QQ==';
      queueMicrotask(() => this.onload());
    }
  };
  window.pywebview = {
    api: {
      begin_blob_save() {
        return { ok: true, transfer_id: 't' };
      },
      append_blob_save() {
        scope.invalidate();
        return true;
      },
      finish_blob_save() {
        actions.push('finish');
        return { ok: true };
      },
      abort_blob_save(id) {
        actions.push(['abort', id]);
        return true;
      },
    },
  };
  try {
    assert.equal(await scope.download(artifact, 'owner', () => true), 'cancelled');
    assert.deepEqual(actions, [['abort', 't']]);
  } finally {
    globalThis.FileReader = originalReader;
  }
});
test('legacy artifact button keeps desktop URL save behavior', async () => {
  const urls = [];
  window.pywebview = {
    api: {
      download_file(url) {
        urls.push(url);
        return true;
      },
    },
  };
  root = createRoot(document.getElementById('root'));
  await act(async () => root.render(React.createElement(ArtifactList)));
  await act(async () => document.querySelector('[data-testid="artifact-list-item-download"]').click());
  assert.deepEqual(urls, [artifact.downloadUrl]);
  assert.equal(calls.length, 0);
});

test('owner button reports a generic refusal without exposing the HTTP error body', async () => {
  const alerts = [], oldAlert = window.alert;
  window.alert = message => alerts.push(message);
  globalThis.fetch = async () => ({ ok: false, blob() { throw Error('sensitive body must not read'); } });
  try {
    await render();
    await act(async () => document.querySelector('[data-testid="artifact-list-item-download"]').click());
    assert.deepEqual(alerts, [en.artifacts.ownerDownloadFailed]);
  } finally { window.alert = oldAlert; }
});

for (const organizationAuth of [true, false]) test(`overview artifact entry respects organization=${organizationAuth}`, async () => {
  const { ToolPanel } = await import(base + 'components/ToolPanel/index.js');
  window.matchMedia = () => ({ matches: true, addEventListener() {}, removeEventListener() {} });
  const runtime = useChatStore.getState().runtimes.owner;
  runtime.messages[0].fileItems[0].name = 'report.md';
  runtime.toolExecutions = new Map();
  useSessionStore.getState().ensureRuntime('owner');
  const navigation = [], selection = [], tabs = [];
  window.jiuwenDesktop = { browser: { navigate: async (...args) => navigation.push(args) } };
  root = createRoot(document.getElementById('root'));
  try {
    const noop = () => {};
    await act(async () => root.render(React.createElement(ArtifactOwnerContext.Provider, { value: { organizationAuth, sessionId: 'owner' } }, React.createElement(ToolPanel, {
      sessionId: 'owner', teamAreaExpanded: false, teamAreaActiveTab: 'overview', teamAreaActiveDetailTab: 'members',
      singleAgentPanelExpanded: false, singleAgentPanelActiveTab: 'tools', setTeamAreaExpanded: noop,
      setTeamAreaActiveTab: noop, setTeamAreaActiveDetailTab: noop, setTeamAreaSelectedMemberId: noop,
      setTeamAreaSelectedArtifactId: noop, setSingleAgentPanelExpanded: noop,
      setSingleAgentPanelActiveTab: value => tabs.push(value), setSingleAgentPanelSelectedArtifactId: value => selection.push(value),
    }))));
    const item = document.querySelector('[data-testid="tool-panel-artifacts"] [data-testid="team-area-task-planning-task-row"]');
    assert.ok(item);
    await act(async () => item.click());
    if (organizationAuth) { assert.equal(navigation.length, 0); assert.equal(selection.length, 1); assert.deepEqual(tabs, ['artifacts']); }
    else { assert.equal(navigation.length, 1); assert.equal(selection.length, 0); }
  } finally { delete window.jiuwenDesktop; }
});
