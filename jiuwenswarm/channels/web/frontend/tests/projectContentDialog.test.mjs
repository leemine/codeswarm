import test from 'node:test';
import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';

const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/' });
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  sessionStorage: dom.window.sessionStorage,
  localStorage: dom.window.localStorage,
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
const { ProjectContentDialog } =
  await import('../node_modules/.cache/project-content/multi-session/sidebar/ProjectContentDialog.js');
const { projectRegistryClient } =
  await import('../node_modules/.cache/project-content/features/workspace/projectRegistryClient.js');
const { webClient } = await import('../node_modules/.cache/project-content/services/webClient.js');
const root = createRoot(document.getElementById('root'));
const find = (id) => document.querySelector(`[data-testid="multi-session-project-content-${id}"]`);
const tick = () =>
  act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
const current = {
  project_id: 'project',
  revision: 2,
  latest_revision: 2,
  can_write: true,
  instructions: 'Project instruction',
  sources: [
    {
      source_id: 'source-one',
      revision: 1,
      title: 'Reference',
      origin: 'file:///private/memory',
      content: '<script>window.leaked = true</script>',
      trust: 'untrusted',
    },
  ],
  versions: [
    { revision: 2, updated_at: 2 },
    { revision: 1, updated_at: 1 },
  ],
};
async function render() {
  await act(async () =>
    root.render(
      React.createElement(ProjectContentDialog, { project: { project_id: 'project', name: 'Research' }, onClose() {} }),
    ),
  );
  await tick();
}
async function unmount() {
  await act(async () => root.render(null));
}

const originalGet = projectRegistryClient.getContent;
const originalUpdate = projectRegistryClient.updateContent;

test('RPCs carry revision and content without asserted identity or trust elevation', async () => {
  const original = webClient.request;
  const calls = [];
  webClient.request = async (method, params) => {
    calls.push({ method, params });
    return current;
  };
  try {
    await originalGet('project', 1);
    await originalUpdate('project', { instructions: '', sources: [], expected_revision: 2 });
    assert.equal(calls[0].method, 'project.content.get');
    assert.deepEqual(calls[0].params, { project_id: 'project', revision: 1 });
    assert.equal(calls[1].method, 'project.content.update');
    assert.deepEqual(calls[1].params, { project_id: 'project', instructions: '', sources: [], expected_revision: 2 });
  } finally {
    webClient.request = original;
  }
});

test('editor renders source as text and saves with current CAS version', async () => {
  let submitted;
  projectRegistryClient.getContent = async () => structuredClone(current);
  projectRegistryClient.updateContent = async (project, data) => {
    submitted = { project, data };
    return {
      ...current,
      revision: 3,
      latest_revision: 3,
      versions: [{ revision: 3, updated_at: 3 }, ...current.versions],
    };
  };
  try {
    await render();
    assert.equal(find('instructions').value, 'Project instruction');
    assert.equal(find('source-body').value, '<script>window.leaked = true</script>');
    assert.equal(document.querySelector('script'), null);
    await act(async () => find('save').click());
    await tick();
    assert.equal(submitted.data.expected_revision, 2);
    assert.equal(submitted.data.sources[0].trust, 'untrusted');
    assert.equal(find('version').value, '3');
    assert.ok(find('saved'));
  } finally {
    await unmount();
  }
});

test('read-only and old versions never expose a save action', async () => {
  projectRegistryClient.getContent = async () => ({ ...current, can_write: false });
  try {
    await render();
    assert.equal(find('save'), null);
    assert.equal(find('instructions').readOnly, true);
    assert.equal(find('source-body').readOnly, true);
  } finally {
    await unmount();
  }
  projectRegistryClient.getContent = async () => ({ ...current, revision: 1, can_write: true });
  try {
    await render();
    assert.equal(find('save'), null);
    assert.equal(find('source-add'), null);
  } finally {
    await unmount();
  }
});

test('conflicting save preserves draft and blocks blind retry until reload', async () => {
  projectRegistryClient.getContent = async () => structuredClone(current);
  let calls = 0;
  projectRegistryClient.updateContent = async () => {
    calls += 1;
    throw Object.assign(new Error('stale'), { code: 'CONFLICT' });
  };
  try {
    await render();
    await act(async () => find('save').click());
    await tick();
    assert.equal(calls, 1);
    assert.equal(find('instructions').value, 'Project instruction');
    assert.equal(find('save'), null);
    assert.equal(find('error').textContent, i18next.t('multiSession.project.content.conflict'));
    await act(async () => find('reload').click());
    await tick();
    assert.ok(find('save'));
    assert.equal(calls, 1);
  } finally {
    await unmount();
  }
});

test('authorization loss removes previously loaded content', async () => {
  projectRegistryClient.getContent = async () => structuredClone(current);
  projectRegistryClient.updateContent = async () => {
    throw Object.assign(new Error('revoked'), { code: 'FORBIDDEN' });
  };
  try {
    await render();
    await act(async () => find('save').click());
    await tick();
    assert.equal(find('instructions'), null);
    assert.equal(find('sources'), null);
    assert.equal(find('error').textContent, i18next.t('multiSession.project.content.forbidden'));
  } finally {
    await unmount();
  }
});

test('late response after project editor unmount cannot publish content', async () => {
  let finish;
  projectRegistryClient.getContent = () =>
    new Promise((resolve) => {
      finish = resolve;
    });
  await render();
  await unmount();
  await act(async () => finish(current));
  await tick();
  assert.equal(find('instructions'), null);
});
