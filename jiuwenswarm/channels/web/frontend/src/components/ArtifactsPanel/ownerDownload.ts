import { saveBlobWithResult, type DesktopSaveOutcome } from '../../utils/desktopSave';
import type { ArtifactItem } from './artifactCollection';

/** Routing only: the server verifies the opaque issuer token and current owner. */
export function ownerDownloadUrl(
  artifact: Pick<ArtifactItem, 'downloadUrl' | 'downloadToken'>,
  sessionId: string,
): string | null {
  if (!sessionId || sessionId === 'new') return null;
  let token = artifact.downloadToken;
  if (artifact.downloadUrl) {
    if (!artifact.downloadUrl.startsWith('/file-api/download?')) return null;
    const url = new URL(artifact.downloadUrl, 'http://owner.invalid');
    if (url.pathname !== '/file-api/download' || url.hash) return null;
    const allowed = new Set(['token', 'session_id', 'inline']);
    for (const key of url.searchParams.keys()) {
      if (!allowed.has(key) || url.searchParams.getAll(key).length !== 1) return null;
    }
    const urlToken = url.searchParams.get('token');
    if (!urlToken || (token && token !== urlToken)) return null;
    const sourceSession = url.searchParams.get('session_id');
    if (sourceSession !== null && sourceSession !== sessionId) return null;
    if (url.searchParams.has('inline') && !['0', '1'].includes(url.searchParams.get('inline')!)) return null;
    token = urlToken;
  }
  if (!token?.trim()) return null;
  return `/file-api/download?${new URLSearchParams({ token, session_id: sessionId })}`;
}

/** One UI operation; it cannot retract bytes already saved by the user. */
export class OwnerDownloadScope {
  private generation = 0;
  private pending = new Set<AbortController>();

  invalidate(): void {
    this.generation += 1;
    for (const controller of this.pending) controller.abort();
    this.pending.clear();
  }

  async download(artifact: ArtifactItem, sessionId: string, current: () => boolean): Promise<DesktopSaveOutcome> {
    const url = ownerDownloadUrl(artifact, sessionId);
    if (!url || !current()) return 'failed';
    const controller = new AbortController();
    const generation = this.generation;
    const isCurrent = () => generation === this.generation && !controller.signal.aborted && current();
    this.pending.add(controller);
    try {
      const response = await fetch(url, {
        method: 'GET',
        credentials: 'same-origin',
        mode: 'same-origin',
        redirect: 'error',
        cache: 'no-store',
        signal: controller.signal,
      });
      if (!isCurrent()) return 'cancelled';
      if (!response.ok || response.redirected) return 'failed';
      const blob = await response.blob();
      if (!isCurrent()) return 'cancelled';
      return (
        await saveBlobWithResult(blob, artifact.name || 'download', {
          signal: controller.signal,
          isCurrent,
        })
      ).outcome;
    } catch {
      return isCurrent() ? 'failed' : 'cancelled';
    } finally {
      controller.abort();
      this.pending.delete(controller);
    }
  }
}
