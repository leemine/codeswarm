import assert from 'node:assert/strict';
import test from 'node:test';
import { organizationRequestRestriction } from '../node_modules/.cache/organization-creation/organizationRelease.js';

test('Single creation allows absent and default projects in both work surfaces', () => {
  for (const organization of [true, false]) {
    for (const project_id of [undefined, '', 'default', 'default_code', 'proj_selected']) {
      for (const mode of ['agent', 'agent.code']) {
        assert.equal(organizationRequestRestriction(organization, 'session.create', {mode, project_id}), null);
      }
    }
  }
});
test('draft entry is still allowed while organization team and goal restrictions remain', () => {
  assert.equal(organizationRequestRestriction(true, 'session.create', { mode: 'agent.code' }), null);
  assert.equal(organizationRequestRestriction(true, 'session.create', { mode: 'team' }), 'organizationRelease.teamUnavailable');
  assert.equal(organizationRequestRestriction(true, 'command.goal', { action: 'set' }), 'organizationRelease.goalUnavailable');
});
