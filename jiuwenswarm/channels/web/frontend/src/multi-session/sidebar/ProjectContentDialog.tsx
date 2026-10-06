import { useEffect, useId, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Dialog, Input } from '../../components/ui';
import {
  projectRegistryClient,
  type ProjectContent,
  type ProjectSource,
} from '../../features/workspace/projectRegistryClient';
import './ProjectContentDialog.css';
import { ProjectResourcesPanel } from './ProjectResourcesPanel';

export function ProjectContentDialog({
  project,
  onClose,
  resourcesEnabled = false,
}: {
  project: { project_id: string; name: string };
  onClose: () => void;
  /** Visibility only; resource RPCs still authorize every operation. */
  resourcesEnabled?: boolean;
}) {
  const { t } = useTranslation();
  const titleId = useId();
  const [tab, setTab] = useState<'content' | 'resources'>('content');
  const showResources = resourcesEnabled && tab === 'resources';
  useEffect(() => {
    if (!resourcesEnabled) setTab('content');
  }, [resourcesEnabled]);
  const requestVersion = useRef(0);
  const [content, setContent] = useState<ProjectContent | null>(null);
  const [instructions, setInstructions] = useState('');
  const [sources, setSources] = useState<ProjectSource[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [saved, setSaved] = useState(false);
  const editable = Boolean(content?.can_write && content.revision === content.latest_revision && !conflict);

  function apply(next: ProjectContent) {
    setContent(next);
    setInstructions(next.instructions);
    setSources(next.sources.map((source) => ({ ...source })));
  }

  async function load(revision?: number) {
    const token = ++requestVersion.current;
    setBusy(true);
    setError(null);
    setConflict(false);
    setSaved(false);
    try {
      const next = await projectRegistryClient.getContent(project.project_id, revision);
      if (token === requestVersion.current) apply(next);
    } catch (failure) {
      if (token !== requestVersion.current) return;
      setContent(null);
      setInstructions('');
      setSources([]);
      const code = (failure as { code?: string }).code;
      setError(
        t(code === 'FORBIDDEN' ? 'multiSession.project.content.forbidden' : 'multiSession.project.content.loadFailed'),
      );
    } finally {
      if (token === requestVersion.current) setBusy(false);
    }
  }

  useEffect(() => {
    void load();
    return () => {
      requestVersion.current += 1;
    };
    // The project identity defines this editor lifetime; draft changes do not reload it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project.project_id]);

  async function save() {
    if (!content || !editable || busy) return;
    const token = ++requestVersion.current;
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const next = await projectRegistryClient.updateContent(project.project_id, {
        instructions,
        sources,
        expected_revision: content.latest_revision,
      });
      if (token === requestVersion.current) {
        apply(next);
        setSaved(true);
      }
    } catch (failure) {
      if (token !== requestVersion.current) return;
      const code = (failure as { code?: string }).code;
      if (code === 'FORBIDDEN') {
        setContent(null);
        setInstructions('');
        setSources([]);
      }
      setConflict(code === 'CONFLICT');
      setError(
        t(
          code === 'CONFLICT'
            ? 'multiSession.project.content.conflict'
            : code === 'FORBIDDEN'
              ? 'multiSession.project.content.forbidden'
              : 'multiSession.project.content.saveFailed',
        ),
      );
    } finally {
      if (token === requestVersion.current) setBusy(false);
    }
  }

  function updateSource(id: string, patch: Partial<ProjectSource>) {
    setSaved(false);
    setSources((current) => current.map((source) => (source.source_id === id ? { ...source, ...patch } : source)));
  }

  return (
    <Dialog open titleId={titleId} onCancel={onClose} closeDisabled={busy}>
      <div
        className="project-content-dialog"
        data-testid="multi-session-project-content"
        data-variant={project.project_id}
      >
        <h2 id={titleId} data-testid="multi-session-project-content-title">
          {t('multiSession.project.content.title', { name: project.name })}
        </h2>
        {resourcesEnabled && (
          <div className="project-content-dialog__actions" data-testid="multi-session-project-tabs">
            <Button
              disabled={busy}
              variant={tab === 'content' ? 'primary' : 'secondary'}
              aria-pressed={tab === 'content'}
              onClick={() => setTab('content')}
              data-testid="multi-session-project-tab-content"
            >
              {t('multiSession.project.resources.contentTab')}
            </Button>
            <Button
              disabled={busy}
              variant={tab === 'resources' ? 'primary' : 'secondary'}
              aria-pressed={tab === 'resources'}
              onClick={() => setTab('resources')}
              data-testid="multi-session-project-tab-resources"
            >
              {t('multiSession.project.resources.tab')}
            </Button>
          </div>
        )}
        {showResources && <ProjectResourcesPanel key={project.project_id} projectId={project.project_id} />}
        <div hidden={showResources} className="project-content-dialog__content">
          <p data-testid="multi-session-project-content-next-turn">{t('multiSession.project.content.nextTurn')}</p>
          {error && (
            <p role="alert" className="project-content-dialog__error" data-testid="multi-session-project-content-error">
              {error}
            </p>
          )}
          {saved && (
            <p role="status" data-testid="multi-session-project-content-saved">
              {t('multiSession.project.content.saved')}
            </p>
          )}
          {busy && (
            <p role="status" data-testid="multi-session-project-content-loading">
              {t('common.loading')}
            </p>
          )}
          {content && (
            <>
              <label
                className="project-content-dialog__field"
                data-testid="multi-session-project-content-version-label"
              >
                {t('multiSession.project.content.version')}
                <select
                  disabled={busy}
                  value={content.revision}
                  onChange={(event) => void load(Number(event.target.value))}
                  data-testid="multi-session-project-content-version"
                >
                  {content.revision === 0 && (
                    <option value={0}>{t('multiSession.project.content.emptyVersion')}</option>
                  )}
                  {content.versions.map((version) => (
                    <option key={version.revision} value={version.revision}>
                      {t('multiSession.project.content.versionNumber', { version: version.revision })}
                    </option>
                  ))}
                </select>
              </label>
              {!editable && (
                <p data-testid="multi-session-project-content-read-only">
                  {t('multiSession.project.content.readOnly')}
                </p>
              )}
              <label
                className="project-content-dialog__field"
                data-testid="multi-session-project-content-instructions-label"
              >
                {t('multiSession.project.content.instructions')}
                <textarea
                  value={instructions}
                  readOnly={!editable}
                  disabled={busy}
                  rows={5}
                  onChange={(event) => {
                    setInstructions(event.target.value);
                    setSaved(false);
                  }}
                  data-testid="multi-session-project-content-instructions"
                />
              </label>
              <h3 data-testid="multi-session-project-content-sources-title">
                {t('multiSession.project.content.sources')}
              </h3>
              <p data-testid="multi-session-project-content-source-trust">
                {t('multiSession.project.content.sourceTrust')}
              </p>
              <div className="project-content-dialog__sources" data-testid="multi-session-project-content-sources">
                {sources.map((source) => (
                  <fieldset
                    key={source.source_id}
                    disabled={busy}
                    data-testid="multi-session-project-content-source"
                    data-variant={source.source_id}
                  >
                    <legend data-testid="multi-session-project-content-source-version">
                      {t('multiSession.project.content.sourceVersion', { version: source.revision || 1 })}
                    </legend>
                    <label
                      className="project-content-dialog__field"
                      data-testid="multi-session-project-content-source-title-label"
                    >
                      {t('multiSession.project.content.sourceTitle')}
                      <Input
                        readOnly={!editable}
                        value={source.title}
                        onChange={(title) => updateSource(source.source_id, { title })}
                        data-testid="multi-session-project-content-source-title"
                      />
                    </label>
                    <label
                      className="project-content-dialog__field"
                      data-testid="multi-session-project-content-source-origin-label"
                    >
                      {t('multiSession.project.content.sourceOrigin')}
                      <Input
                        readOnly={!editable}
                        value={source.origin}
                        onChange={(origin) => updateSource(source.source_id, { origin })}
                        data-testid="multi-session-project-content-source-origin"
                      />
                    </label>
                    <label
                      className="project-content-dialog__field"
                      data-testid="multi-session-project-content-source-body-label"
                    >
                      {t('multiSession.project.content.sourceBody')}
                      <textarea
                        readOnly={!editable}
                        rows={4}
                        value={source.content}
                        onChange={(event) => updateSource(source.source_id, { content: event.target.value })}
                        data-testid="multi-session-project-content-source-body"
                      />
                    </label>
                    {editable && (
                      <Button
                        onClick={() => {
                          setSources((items) => items.filter((item) => item.source_id !== source.source_id));
                          setSaved(false);
                        }}
                        data-testid="multi-session-project-content-source-remove"
                      >
                        {t('common.delete')}
                      </Button>
                    )}
                  </fieldset>
                ))}
              </div>
              {editable && (
                <Button
                  disabled={busy || sources.length >= 32}
                  onClick={() => {
                    setSources((items) => [
                      ...items,
                      {
                        source_id: crypto.randomUUID(),
                        revision: 0,
                        title: '',
                        origin: '',
                        content: '',
                        trust: 'untrusted',
                      },
                    ]);
                    setSaved(false);
                  }}
                  data-testid="multi-session-project-content-source-add"
                >
                  {t('multiSession.project.content.addSource')}
                </Button>
              )}
            </>
          )}
        </div>
        <footer className="project-content-dialog__actions" data-testid="multi-session-project-content-actions">
          <Button disabled={busy} onClick={onClose} data-testid="multi-session-project-content-close">
            {t('common.close')}
          </Button>
          {!showResources && (
            <Button disabled={busy} onClick={() => void load()} data-testid="multi-session-project-content-reload">
              {t('multiSession.project.content.reload')}
            </Button>
          )}
          {!showResources && editable && (
            <Button
              variant="primary"
              loading={busy}
              onClick={() => void save()}
              data-testid="multi-session-project-content-save"
            >
              {t('common.save')}
            </Button>
          )}
        </footer>
      </div>
    </Dialog>
  );
}
