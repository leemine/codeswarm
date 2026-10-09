import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useTranslation,
} from '../../../channels/web/frontend/src/applicationPlugins/ui';
import { Button, Input, Textarea } from '../../../channels/web/frontend/src/components/ui';
import { evaluationRequest as request } from './client';
import type { Catalog, ImportPreview, Task, TaskRef, TaskVersion } from './types';
import './evaluation.css';
import Experiments from './Experiments';

const emptyTask = (): Task => ({
  schema_version: 1,
  task_id: `task-${crypto.randomUUID()}`,
  name: '',
  instruction: '',
  files: [],
  deliverables: [],
  acceptance: { kind: 'manual', script: '', timeout_seconds: 30 },
});

export default function EvaluationApp() {
  const { t } = useTranslation();
  const [unavailable, setUnavailable] = useState(false);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [task, setTask] = useState<Task>(emptyTask);
  const [draftRevision, setDraftRevision] = useState(0);
  const [readOnly, setReadOnly] = useState(false);
  const [filePath, setFilePath] = useState('');
  const [fileContent, setFileContent] = useState('');
  const [jsonl, setJsonl] = useState('');
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [selected, setSelected] = useState<TaskRef[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);
  const refresh = useCallback(async () => {
    try {
      const value = await request<Catalog>('catalog');
      if (mounted.current) {
        setCatalog(value);
        setUnavailable(false);
      }
    } catch (err) {
      if (mounted.current) {
        setCatalog(null);
        setUnavailable((err as { code?: string }).code === 'FORBIDDEN');
      }
      throw err;
    }
  }, []);
  const act = useCallback(async (operation: () => Promise<void>) => {
    setBusy(true);
    setError('');
    try {
      await operation();
    } catch (err) {
      if (mounted.current)
        setError(
          (err as { message?: string; code?: string }).code || (err as Error).message || 'EVALUATION_UNAVAILABLE',
        );
    } finally {
      if (mounted.current) setBusy(false);
    }
  }, []);
  useEffect(() => {
    mounted.current = true;
    void act(refresh);
    return () => {
      mounted.current = false;
    };
  }, [act, refresh]);
  if (!catalog) {
    return (
      <main className="evaluation-app" data-testid="evaluation-app">
        <header className="evaluation-header" data-testid="evaluation-header">
          <h1 data-testid="evaluation-title">{t('evaluation.title')}</h1>
          <Button data-testid="evaluation-refresh" disabled={busy} onClick={() => void act(refresh)}>
            {t('evaluation.refresh')}
          </Button>
        </header>
        {unavailable ? (
          <p role="status" data-testid="evaluation-unavailable">
            {t('evaluation.unavailable')}
          </p>
        ) : error ? (
          <p role="alert" className="evaluation-error" data-testid="evaluation-error">
            {error}
          </p>
        ) : null}
      </main>
    );
  }
  const change = (value: Partial<Task>) => setTask((current) => ({ ...current, ...value }));
  const edit = (value: Task, revision: number, published = false) => {
    setTask(value);
    setDraftRevision(revision);
    setReadOnly(published);
  };
  const toggle = (version: TaskVersion) =>
    setSelected((current) =>
      current.some((item) => item.task_id === version.id && item.revision === version.revision)
        ? current.filter((item) => item.task_id !== version.id || item.revision !== version.revision)
        : [...current, { task_id: version.id, revision: version.revision }],
    );
  return (
    <main className="evaluation-app" data-testid="evaluation-app">
      <header className="evaluation-header" data-testid="evaluation-header">
        <div>
          <h1 data-testid="evaluation-title">{t('evaluation.title')}</h1>
          <p data-testid="evaluation-shared-environment">{t('evaluation.overview')}</p>
        </div>
        <Button data-testid="evaluation-refresh" disabled={busy} onClick={() => void act(refresh)}>
          {t('evaluation.refresh')}
        </Button>
      </header>
      {error && (
        <p role="alert" className="evaluation-error" data-testid="evaluation-error">
          {error}
        </p>
      )}
      <div className="evaluation-columns">
        <section className="evaluation-panel" data-testid="evaluation-library">
          <h2 data-testid="evaluation-library-title">{t('evaluation.library')}</h2>
          <div className="evaluation-actions">
            <Button data-testid="evaluation-new-task" onClick={() => edit(emptyTask(), 0)}>
              {t('evaluation.newTask')}
            </Button>
            <Button
              data-testid="evaluation-load-examples"
              disabled={busy}
              onClick={() =>
                void act(async () => {
                  await request('examples');
                  await refresh();
                })
              }
            >
              {t('evaluation.examples')}
            </Button>
          </div>
          <h3 data-testid="evaluation-datasets-title">{t('evaluation.datasets')}</h3>
          <div data-testid="evaluation-datasets">
            {catalog?.datasets.map((dataset) => (
              <Button
                key={`${dataset.id}:${dataset.revision}`}
                data-testid="evaluation-dataset"
                data-variant={`${dataset.id}:${dataset.revision}`}
                onClick={() => setSelected(dataset.value.tasks)}
              >
                {dataset.value.name} · v{dataset.revision}
              </Button>
            ))}
          </div>
          <h3 data-testid="evaluation-published-title">{t('evaluation.published')}</h3>
          <ul className="evaluation-list" data-testid="evaluation-task-list">
            {catalog?.tasks.map((version) => (
              <li
                key={`${version.id}:${version.revision}`}
                data-testid="evaluation-task-item"
                data-variant={`${version.id}:${version.revision}`}
              >
                <label>
                  <input
                    type="checkbox"
                    data-testid="evaluation-task-select"
                    checked={selected.some((item) => item.task_id === version.id && item.revision === version.revision)}
                    onChange={() => toggle(version)}
                  />
                  <span>
                    {version.value.name} · v{version.revision}
                  </span>
                </label>
                <Button
                  size="sm"
                  data-testid="evaluation-task-view"
                  onClick={() =>
                    edit(
                      version.value,
                      catalog.drafts.find((item) => item.task.task_id === version.id)?.draft_revision || 0,
                      true,
                    )
                  }
                >
                  {t('evaluation.view')}
                </Button>
              </li>
            ))}
          </ul>
          <h3 data-testid="evaluation-drafts-title">{t('evaluation.drafts')}</h3>
          <ul className="evaluation-list" data-testid="evaluation-drafts">
            {catalog?.drafts.map((draft) => (
              <li key={draft.task.task_id} data-testid="evaluation-draft" data-variant={draft.task.task_id}>
                <Button data-testid="evaluation-draft-edit" onClick={() => edit(draft.task, draft.draft_revision)}>
                  {draft.task.name}
                </Button>
              </li>
            ))}
          </ul>
        </section>
        <section className="evaluation-panel" data-testid="evaluation-editor">
          <h2 data-testid="evaluation-editor-title">{t('evaluation.taskEditor')}</h2>
          {readOnly && (
            <p data-testid="evaluation-version-readonly">
              {t('evaluation.readonly')}{' '}
              <Button data-testid="evaluation-new-version" onClick={() => setReadOnly(false)}>
                {t('evaluation.newVersion')}
              </Button>
            </p>
          )}
          <fieldset disabled={readOnly || busy} data-testid="evaluation-task-fields">
            <label>
              {t('evaluation.name')}
              <Input data-testid="evaluation-task-name" value={task.name} onChange={(name) => change({ name })} />
            </label>
            <label>
              {t('evaluation.instruction')}
              <Textarea
                data-testid="evaluation-task-instruction"
                rows={5}
                value={task.instruction}
                onChange={(instruction) => change({ instruction })}
              />
            </label>
            <label>
              {t('evaluation.filePath')}
              <Input data-testid="evaluation-file-path" value={filePath} onChange={setFilePath} />
            </label>
            <label>
              {t('evaluation.fileContent')}
              <Textarea data-testid="evaluation-file-content" rows={3} value={fileContent} onChange={setFileContent} />
            </label>
            <Button
              data-testid="evaluation-file-add"
              disabled={!filePath}
              onClick={() => {
                change({
                  files: [
                    ...(task.files || []).filter((file) => file.path !== filePath),
                    { path: filePath, content: fileContent },
                  ],
                });
                setFilePath('');
                setFileContent('');
              }}
            >
              {t('evaluation.addFile')}
            </Button>
            <ul data-testid="evaluation-files">
              {task.files?.map((file) => (
                <li key={file.path} data-testid="evaluation-file" data-variant={file.path}>
                  <span>{file.path}</span>
                  <Button
                    size="sm"
                    data-testid="evaluation-file-remove"
                    onClick={() =>
                      change({
                        files: task.files?.filter((item) => item.path !== file.path),
                      })
                    }
                  >
                    {t('evaluation.remove')}
                  </Button>
                </li>
              ))}
            </ul>
            <label>
              {t('evaluation.deliverables')}
              <Input
                data-testid="evaluation-deliverables"
                value={(task.deliverables || []).join(', ')}
                onChange={(value) =>
                  change({
                    deliverables: value
                      .split(',')
                      .map((item) => item.trim())
                      .filter(Boolean),
                  })
                }
              />
            </label>
            <label>
              {t('evaluation.acceptance')}
              <Textarea
                data-testid="evaluation-acceptance-script"
                rows={5}
                value={task.acceptance?.script || ''}
                onChange={(script) =>
                  change({
                    acceptance: {
                      ...task.acceptance,
                      kind: script.trim() ? 'python' : 'manual',
                      script,
                      timeout_seconds: 30,
                    },
                  })
                }
              />
            </label>
            <label>
              {t('evaluation.dependencyLock')}
              <Input
                data-testid="evaluation-dependency-lock"
                value={task.acceptance?.dependency_lock || ''}
                onChange={(value) =>
                  change({
                    acceptance: {
                      ...task.acceptance,
                      kind: task.acceptance?.kind || 'manual',
                      dependency_lock: value || null,
                    },
                  })
                }
              />
            </label>
            <p data-testid="evaluation-manual-review-hint">{t('evaluation.manualHint')}</p>
            <div className="evaluation-actions">
              <Button
                variant="primary"
                data-testid="evaluation-save-draft"
                disabled={!task.name.trim() || !task.instruction.trim()}
                onClick={() =>
                  void act(async () => {
                    const saved = await request<{
                      draft_revision: number;
                      task: Task;
                    }>('task.save', { task, expected_revision: draftRevision });
                    edit(saved.task, saved.draft_revision);
                    await refresh();
                  })
                }
              >
                {t('evaluation.save')}
              </Button>
              <Button
                data-testid="evaluation-publish"
                disabled={!draftRevision}
                onClick={() =>
                  void act(async () => {
                    const saved = await request<{
                      draft_revision: number;
                      task: Task;
                    }>('task.save', { task, expected_revision: draftRevision });
                    setDraftRevision(saved.draft_revision);
                    await request('task.publish', {
                      task_id: task.task_id,
                      draft_revision: saved.draft_revision,
                    });
                    setReadOnly(true);
                    await refresh();
                  })
                }
              >
                {t('evaluation.publish')}
              </Button>
            </div>
          </fieldset>
        </section>
        <section className="evaluation-panel" data-testid="evaluation-import">
          <h2 data-testid="evaluation-import-title">{t('evaluation.import')}</h2>
          <p data-testid="evaluation-import-hint">{t('evaluation.importHint')}</p>
          <input
            type="file"
            accept=".jsonl,.json"
            data-testid="evaluation-import-file"
            aria-label={t('evaluation.import')}
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file)
                void act(async () => {
                  if (file.size > 2 * 1024 * 1024) throw new Error('IMPORT_TOO_LARGE');
                  setJsonl(await file.text());
                  setPreview(null);
                });
            }}
          />
          <Textarea
            data-testid="evaluation-import-text"
            rows={9}
            value={jsonl}
            onChange={(value) => {
              setJsonl(value);
              setPreview(null);
            }}
          />
          <div className="evaluation-actions">
            <Button
              data-testid="evaluation-import-preview"
              disabled={busy || !jsonl.trim()}
              onClick={() =>
                void act(async () =>
                  setPreview(
                    await request<ImportPreview>('import.preview', {
                      text: jsonl,
                    }),
                  ),
                )
              }
            >
              {t('evaluation.preview')}
            </Button>
            <Button
              data-testid="evaluation-import-commit"
              disabled={busy || !preview?.can_import}
              onClick={() =>
                void act(async () => {
                  setPreview(
                    await request<ImportPreview>('import.commit', {
                      text: jsonl,
                    }),
                  );
                  await refresh();
                })
              }
            >
              {t('evaluation.importSave')}
            </Button>
          </div>
          <ul data-testid="evaluation-import-rows">
            {preview?.rows.map((row) => (
              <li key={row.line} data-testid="evaluation-import-row" data-variant={row.line}>
                {t('evaluation.line', { line: row.line })}:{' '}
                {row.errors.length
                  ? row.errors.map((item) => `${item.field}: ${item.code}`).join(', ')
                  : row.task?.name}
              </li>
            ))}
          </ul>
        </section>
      </div>
      <Experiments selected={selected} onSelect={setSelected} />
    </main>
  );
}
