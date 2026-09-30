export const SURFACE_CAPABILITY_IDS = [
  'documents',
  'web',
  'artifacts',
  'filesystem',
  'terminal',
  'git',
  'diff',
  'test',
  'review',
  'lsp',
  'browser',
  'subagents',
  'memory',
] as const;

export type SurfaceCapabilityId = (typeof SURFACE_CAPABILITY_IDS)[number];
export type SurfaceCapabilityState =
  | 'available'
  | 'unavailable'
  | 'needs_install'
  | 'needs_auth'
  | 'degraded'
  | 'not_applicable';

export interface SurfaceCapabilityEntry {
  id: SurfaceCapabilityId;
  state: SurfaceCapabilityState;
  reason_code: string;
  reason: string;
  requires_authorization: boolean;
}

export interface SurfaceCapabilityManifest {
  schema_version: 1;
  provider_id: string;
  surface: 'work' | 'code';
  state: 'available' | 'degraded' | 'unavailable';
  restart_required: boolean;
  entries: SurfaceCapabilityEntry[];
}

const CAPABILITY_ID_SET = new Set<string>(SURFACE_CAPABILITY_IDS);
const ENTRY_STATES = new Set<string>([
  'available',
  'unavailable',
  'needs_install',
  'needs_auth',
  'degraded',
  'not_applicable',
]);
const AGGREGATE_STATES = new Set<string>(['available', 'degraded', 'unavailable']);

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

export function parseSurfaceCapabilityManifest(value: unknown): SurfaceCapabilityManifest | null {
  const raw = record(value);
  if (
    !raw
    || raw.schema_version !== 1
    || typeof raw.provider_id !== 'string'
    || !raw.provider_id.trim()
    || (raw.surface !== 'work' && raw.surface !== 'code')
    || typeof raw.state !== 'string'
    || !AGGREGATE_STATES.has(raw.state)
    || typeof raw.restart_required !== 'boolean'
    || !Array.isArray(raw.entries)
    || raw.entries.length !== SURFACE_CAPABILITY_IDS.length
  ) return null;

  const entries: SurfaceCapabilityEntry[] = [];
  const seen = new Set<string>();
  for (const valueEntry of raw.entries) {
    const entry = record(valueEntry);
    if (
      !entry
      || typeof entry.id !== 'string'
      || !CAPABILITY_ID_SET.has(entry.id)
      || seen.has(entry.id)
      || typeof entry.state !== 'string'
      || !ENTRY_STATES.has(entry.state)
      || typeof entry.reason_code !== 'string'
      || typeof entry.reason !== 'string'
      || typeof entry.requires_authorization !== 'boolean'
    ) return null;
    const carriesReason = Boolean(entry.reason_code || entry.reason);
    const reasonAllowed = entry.state !== 'available' && entry.state !== 'not_applicable';
    if (carriesReason !== reasonAllowed) return null;
    seen.add(entry.id);
    entries.push(entry as unknown as SurfaceCapabilityEntry);
  }
  if (SURFACE_CAPABILITY_IDS.some(id => !seen.has(id))) return null;

  return {
    schema_version: 1,
    provider_id: raw.provider_id,
    surface: raw.surface,
    state: raw.state as SurfaceCapabilityManifest['state'],
    restart_required: raw.restart_required,
    entries,
  };
}

export function getSurfaceCapability(
  manifest: SurfaceCapabilityManifest | null | undefined,
  capabilityId: SurfaceCapabilityId,
): SurfaceCapabilityEntry | null {
  return manifest?.entries.find(entry => entry.id === capabilityId) ?? null;
}

export function isSurfaceCapabilityUsable(
  manifest: SurfaceCapabilityManifest | null | undefined,
  capabilityId: SurfaceCapabilityId,
): boolean {
  const state = getSurfaceCapability(manifest, capabilityId)?.state;
  return state === undefined || state === 'available' || state === 'degraded';
}

export function resolveCodeSurfaceAvailability(
  manifest: SurfaceCapabilityManifest | null | undefined,
  hasCodeSession: boolean,
): { git: boolean; diff: boolean; review: boolean; visible: boolean } {
  const git = isSurfaceCapabilityUsable(manifest, 'git');
  const diff = isSurfaceCapabilityUsable(manifest, 'diff');
  const review = hasCodeSession && isSurfaceCapabilityUsable(manifest, 'review');
  return {
    git,
    diff,
    review,
    visible: hasCodeSession && (git || diff || review),
  };
}

export function manifestWarnings(
  manifest: SurfaceCapabilityManifest | null | undefined,
): SurfaceCapabilityEntry[] {
  if (!manifest) return [];
  return manifest.entries.filter(entry =>
    entry.state !== 'available' && entry.state !== 'not_applicable'
  );
}
