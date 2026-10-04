import { useMemo } from 'react';
import { Download } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { useChatStore } from '../../stores';
import { executeDesktopSave, type DesktopSaveApiResult } from '../../utils/desktopSave';
import { FileIcon } from '../FileIcon';
import { buildArtifacts, type ArtifactItem } from './artifactCollection';
import { artifactDownloadUrl } from './filePreviewModel';
import { openFileInDesktopBrowser } from '../../features/desktopBrowserFile';
import { useArtifactOwnerDownload } from './ArtifactOwnerContext';
import { ownerDownloadUrl } from './ownerDownload';

export { fileArtifactId } from './artifactCollection';
export { ArtifactExpandedPanel } from './ArtifactExpandedPanel';

type DownloadCapableWindow = Window & {
  pywebview?: {
    api?: {
      download_file?: (url: string, filename: string) => DesktopSaveApiResult;
    };
  };
};

export function useSessionArtifacts(): ArtifactItem[] {
  const activeSessionId = useChatStore(s => s.activeSessionId);
  const messages = useChatStore(s => s.runtimes[activeSessionId ?? '']?.messages ?? []);

  return useMemo(() => buildArtifacts(messages), [messages]);
}

export function useSessionArtifactsCount(): number {
  return useSessionArtifacts().length;
}

export function ArtifactList({
  onSelectArtifact,
  className,
}: {
  selectedArtifactId?: string;
  onSelectArtifact?: (artifactId: string) => void;
  className?: string;
}) {
  const { t } = useTranslation();
  const artifacts = useSessionArtifacts();
  const owner = useArtifactOwnerDownload();

  const handleDownload = async (artifact: ArtifactItem) => {
    if (owner.organizationAuth) {
      const outcome = await owner.download(artifact);
      if (outcome === 'failed') window.alert(t('artifacts.ownerDownloadFailed'));
      return;
    }
    const downloadUrl = artifactDownloadUrl(artifact);
    if (!downloadUrl) return;

    const pywebviewApi = (window as DownloadCapableWindow).pywebview?.api;
    if (pywebviewApi?.download_file) {
      const outcome = await executeDesktopSave(() => pywebviewApi.download_file!(downloadUrl, artifact.name || 'download'));
      if (outcome === 'failed') {
        window.alert(t('artifacts.downloadFailed', { name: artifact.name }));
      }
      return;
    }

    const link = document.createElement('a');
    link.href = downloadUrl;
    link.download = artifact.name || '';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  return (
    <div className={clsx('min-h-0 overflow-y-auto', className)} data-testid="artifact-list-scroll">
      {owner.organizationAuth && (
        <p className="px-2 text-sm text-text-muted" data-testid="artifact-owner-download-notice">
          {t('artifacts.ownerDownloadNotice')}
        </p>
      )}
      {artifacts.length === 0 ? (
        <div className="flex h-full items-center justify-center px-5 text-center text-sm text-text-muted" data-testid="artifact-list-empty">
          {t('artifacts.empty')}
        </div>
      ) : (
        <div className="space-y-2" data-testid="artifact-list-items">
          {artifacts.map(artifact => {
            return (
              <div
                key={artifact.id}
                className="group flex h-9 w-full min-w-0 items-center gap-2 rounded-md px-2 text-sm text-text hover:bg-[var(--color-tool-tab-active-bg)]"
                data-testid="artifact-list-item"
                data-variant={artifact.id}
                onClick={() => {
                  if (!owner.organizationAuth && openFileInDesktopBrowser({ name: artifact.name, download_url: artifact.downloadUrl })) return;
                  onSelectArtifact?.(artifact.id);
                }}
                role="button"
                tabIndex={0}
                onKeyDown={e => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    if (!owner.organizationAuth && openFileInDesktopBrowser({ name: artifact.name, download_url: artifact.downloadUrl })) return;
                    onSelectArtifact?.(artifact.id);
                  }
                }}
              >
                <FileIcon fileName={artifact.name} size={16} className="shrink-0" />
                <span className="min-w-0 flex-1 truncate" data-testid="artifact-list-item-name">
                  {artifact.name}
                </span>
                <button
                  type="button"
                  className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-text-muted opacity-0 group-hover:opacity-100 hover:bg-secondary hover:text-text disabled:cursor-not-allowed disabled:opacity-40"
                  title={t(owner.organizationAuth && !ownerDownloadUrl(artifact, owner.sessionId)
                    ? 'artifacts.ownerDownloadUnavailable' : 'artifacts.download')}
                  aria-label={t('artifacts.download')}
                  data-testid="artifact-list-item-download"
                  disabled={owner.organizationAuth ? !ownerDownloadUrl(artifact, owner.sessionId) : !artifact.downloadUrl && !artifact.path}
                  onClick={e => {
                    e.stopPropagation();
                    void handleDownload(artifact);
                  }}
                >
                  <Download size={14} />
                </button>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

