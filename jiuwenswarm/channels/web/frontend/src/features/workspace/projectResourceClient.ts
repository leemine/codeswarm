import { webRequest } from '../../services/webClient';

export type ResourceKind = 'workspace' | 'tool' | 'credential' | 'process';
export interface ProjectResourceGrant {
  target_actor: string;
  actions: string[];
  expires_at: number | null;
  can_revoke: boolean;
  state: 'active' | 'unavailable';
}
export interface ProjectResource {
  resource_id: string;
  kind: ResourceKind;
  actions: string[];
  can_grant: boolean;
  expires_at: number | null;
  grants: ProjectResourceGrant[];
}
export interface ProjectResources {
  project_id: string;
  acl_revision: number;
  resource_revision: number;
  resources: ProjectResource[];
}
export interface ResourceMutationInput {
  project_id: string;
  resource_id: string;
  target_actor: string;
  expected_acl_revision: number;
  expected_resource_revision: number;
}
export interface ResourceGrantInput extends ResourceMutationInput {
  actions: string[];
  expires_at: number | null;
}
export interface ResourceMutation {
  committed: true;
  project_id: string;
  resource_id: string;
  target_actor: string;
  resource_revision: number;
}

const actionsByKind: Record<ResourceKind, string[]> = {
  workspace: ['read', 'write'],
  tool: ['invoke'],
  credential: ['use'],
  process: ['execute'],
};
const text = (value: unknown): value is string =>
  typeof value === 'string' &&
  value.length > 0 &&
  value.length <= 256 &&
  value.trim() === value &&
  !/[\x00-\x1f]/.test(value);
const revision = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;
const expiry = (value: unknown): boolean =>
  value === null || (typeof value === 'number' && value > 0 && Number.isFinite(new Date(value * 1000).getTime()));
const keys = (value: unknown, expected: string[]): value is Record<string, unknown> =>
  Boolean(
    value &&
    typeof value === 'object' &&
    !Array.isArray(value) &&
    Object.keys(value).length === expected.length &&
    Object.keys(value).every((key) => expected.includes(key)),
  );
const actions = (value: unknown, allowed: string[], empty = false): value is string[] =>
  Array.isArray(value) &&
  (empty || value.length > 0) &&
  value.length <= allowed.length &&
  value.every((action) => typeof action === 'string' && allowed.includes(action)) &&
  new Set(value).size === value.length;
const unavailable = () => new Error('Resource response unavailable');

function parseResources(value: unknown, projectId: string): ProjectResources {
  if (
    !keys(value, ['project_id', 'acl_revision', 'resource_revision', 'resources']) ||
    value.project_id !== projectId ||
    !revision(value.acl_revision) ||
    !revision(value.resource_revision) ||
    !Array.isArray(value.resources) ||
    value.resources.length > 1000
  )
    throw unavailable();
  const ids = new Set<string>();
  for (const item of value.resources) {
    if (
      !keys(item, ['resource_id', 'kind', 'actions', 'can_grant', 'expires_at', 'grants']) ||
      !text(item.resource_id) ||
      ids.has(item.resource_id) ||
      typeof item.kind !== 'string' ||
      !Object.prototype.hasOwnProperty.call(actionsByKind, item.kind) ||
      typeof item.can_grant !== 'boolean' ||
      !expiry(item.expires_at) ||
      !Array.isArray(item.grants) ||
      item.grants.length > 1000
    )
      throw unavailable();
    ids.add(item.resource_id);
    const allowed = actionsByKind[item.kind as ResourceKind];
    if (!actions(item.actions, allowed, true) || (item.can_grant && item.actions.length === 0)) throw unavailable();
    const actors = new Set<string>();
    for (const grant of item.grants) {
      if (
        !keys(grant, ['target_actor', 'actions', 'expires_at', 'can_revoke', 'state']) ||
        !text(grant.target_actor) ||
        actors.has(grant.target_actor) ||
        !actions(grant.actions, allowed, grant.state === 'unavailable') ||
        !expiry(grant.expires_at) ||
        typeof grant.can_revoke !== 'boolean' ||
        !['active', 'unavailable'].includes(grant.state as string)
      )
        throw unavailable();
      actors.add(grant.target_actor);
    }
  }
  // Return only the validated wire DTO. No credential/reference fields are accepted.
  return value as unknown as ProjectResources;
}

function parseMutation(value: unknown, input: ResourceMutationInput): ResourceMutation {
  if (
    !keys(value, ['committed', 'project_id', 'resource_id', 'target_actor', 'resource_revision']) ||
    value.committed !== true ||
    value.project_id !== input.project_id ||
    value.resource_id !== input.resource_id ||
    value.target_actor !== input.target_actor ||
    value.resource_revision !== input.expected_resource_revision + 1
  )
    throw unavailable();
  return Object.freeze({ ...value }) as unknown as ResourceMutation;
}

export class ResourceMutationCommittedError extends Error {
  constructor(readonly mutation: ResourceMutation) {
    super('Resource mutation committed; exit unconfirmed');
  }
}

async function mutate(
  method: 'grant' | 'revoke',
  input: ResourceMutationInput | ResourceGrantInput,
  signal?: AbortSignal,
) {
  const fields = ['project_id', 'resource_id', 'target_actor', 'expected_acl_revision', 'expected_resource_revision'];
  if (
    !keys(input, method === 'grant' ? [...fields, 'actions', 'expires_at'] : fields) ||
    !text(input.project_id) ||
    !text(input.resource_id) ||
    !text(input.target_actor) ||
    !revision(input.expected_acl_revision) ||
    !revision(input.expected_resource_revision) ||
    (method === 'grant' &&
      (!actions(input.actions, ['read', 'write', 'invoke', 'use', 'execute']) || !expiry(input.expires_at)))
  )
    throw new Error('Invalid resource mutation');
  const frozen = { ...input, ...(method === 'grant' ? { actions: [...(input as ResourceGrantInput).actions] } : {}) };
  let requestId: string | undefined;
  let result: unknown;
  try {
    result = await webRequest(`project.resources.${method}`, frozen, {
      signal,
      onRequestId: (id) => {
        requestId = id;
      },
    });
  } catch (error) {
    const failure = error as { code?: string; requestId?: string; payload?: Record<string, unknown> };
    const payload = failure?.payload;
    if (
      requestId &&
      failure.requestId === requestId &&
      failure.code === 'EXIT_UNCONFIRMED' &&
      payload?.code === 'EXIT_UNCONFIRMED' &&
      payload.exit_confirmed === false &&
      Object.keys(payload).every((key) => ['code', 'error', 'mutation', 'exit_confirmed'].includes(key))
    ) {
      throw new ResourceMutationCommittedError(parseMutation(payload.mutation, frozen));
    }
    throw error;
  }
  if (!keys(result, ['mutation'])) throw unavailable();
  return { mutation: parseMutation(result.mutation, frozen) };
}

export const projectResourceClient = {
  list: async (projectId: string, signal?: AbortSignal) => {
    if (!text(projectId)) throw new Error('Invalid project');
    return parseResources(await webRequest('project.resources.list', { project_id: projectId }, { signal }), projectId);
  },
  grant: (input: ResourceGrantInput, signal?: AbortSignal) => mutate('grant', input, signal),
  revoke: (input: ResourceMutationInput, signal?: AbortSignal) => mutate('revoke', input, signal),
};
