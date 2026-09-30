import assert from 'node:assert/strict';
import test from 'node:test';

import {
  getSurfaceCapability,
  isSurfaceCapabilityUsable,
  manifestWarnings,
  parseSurfaceCapabilityManifest,
  resolveCodeSurfaceAvailability,
} from '../node_modules/.cache/surface-capability-manifest/surfaceCapabilityManifest.mjs';

const ids = [
  'documents', 'web', 'artifacts', 'filesystem', 'terminal', 'git', 'diff',
  'test', 'review', 'lsp', 'browser', 'subagents', 'memory',
];

function manifest() {
  return {
    schema_version: 1,
    provider_id: 'codex',
    surface: 'code',
    state: 'degraded',
    restart_required: false,
    entries: ids.map(id => ({
      id,
      state: id === 'browser' ? 'needs_install' : id === 'documents' || id === 'web' || id === 'artifacts' ? 'not_applicable' : 'available',
      reason_code: id === 'browser' ? 'not_installed' : '',
      reason: id === 'browser' ? 'Browser runtime is not installed' : '',
      requires_authorization: id === 'terminal',
    })),
  };
}

test('strictly parses the complete six-state manifest contract', () => {
  const parsed = parseSurfaceCapabilityManifest(manifest());
  assert.ok(parsed);
  assert.equal(getSurfaceCapability(parsed, 'browser')?.state, 'needs_install');
  assert.equal(isSurfaceCapabilityUsable(parsed, 'terminal'), true);
  assert.equal(isSurfaceCapabilityUsable(parsed, 'browser'), false);
  assert.deepEqual(manifestWarnings(parsed).map(entry => entry.id), ['browser']);
});

test('rejects partial, duplicate, and inconsistent manifests', () => {
  const partial = manifest();
  partial.entries.pop();
  assert.equal(parseSurfaceCapabilityManifest(partial), null);

  const duplicate = manifest();
  duplicate.entries[1] = { ...duplicate.entries[0] };
  assert.equal(parseSurfaceCapabilityManifest(duplicate), null);

  const badReason = manifest();
  badReason.entries.find(entry => entry.id === 'terminal').reason = 'should not exist';
  assert.equal(parseSurfaceCapabilityManifest(badReason), null);
});

test('keeps Git and Diff usable when Review is unavailable', () => {
  const value = manifest();
  value.entries.find(entry => entry.id === 'review').state = 'unavailable';
  value.entries.find(entry => entry.id === 'review').reason_code = 'provider_unsupported';
  value.entries.find(entry => entry.id === 'review').reason = 'Review is unsupported';
  const parsed = parseSurfaceCapabilityManifest(value);
  assert.ok(parsed);
  assert.deepEqual(resolveCodeSurfaceAvailability(parsed, true), {
    git: true,
    diff: true,
    review: false,
    visible: true,
  });
  assert.equal(resolveCodeSurfaceAvailability(parsed, false).visible, false);
});
