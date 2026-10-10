import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useTranslation,
} from '../../../channels/web/frontend/src/applicationPlugins/ui';
import type { ApplicationPluginPageProps } from '../../../channels/web/frontend/src/applicationPlugins/types';
import { Button, Dialog, Input, Textarea } from '../../../channels/web/frontend/src/components/ui';
import { projectRegistryClient } from '../../../channels/web/frontend/src/features/workspace/projectRegistryClient';
import type { Session, ProjectInfo } from '../../../channels/web/frontend/src/types';
import { taskboardClient as api } from './client';
import type { Page, Status, Task } from './types';
import './taskboard.css';
const statuses: Status[] = ['todo', 'doing', 'done'];
const priorities = ['high', 'normal', 'low'] as const;
const blankPages = (): Record<Status, Page> => ({
  todo: { tasks: [], next_cursor: null },
  doing: { tasks: [], next_cursor: null },
  done: { tasks: [], next_cursor: null },
});

export default function TaskboardApp({ taskId, onOpenTask, onOpenSession }: ApplicationPluginPageProps) {
  const { t, i18n } = useTranslation();
  const [pages, setPages] = useState(blankPages);
  const [query, setQuery] = useState('');
  const [project, setProject] = useState('');
  const [projects, setProjects] = useState<ProjectInfo[]>([]);
  const [task, setTask] = useState<Task | null>(null);
  const [result, setResult] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [form, setForm] = useState<{
    title: string;
    description: string;
    priority: string;
    project_id: string;
  } | null>(null);
  const [editing, setEditing] = useState(false);
  const createKey = useRef('');
  const generation = useRef(0);
  const selectedRef = useRef(taskId);
  selectedRef.current = taskId;
  const [picker, setPicker] = useState(false);
  const [sessionProject, setSessionProject] = useState('default_code');
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sessionLimit, setSessionLimit] = useState(30);
  const [sessionTotal, setSessionTotal] = useState(0);
  const [sessionLoading, setSessionLoading] = useState(false);
  const [picked, setPicked] = useState('');
  const dirty = Boolean(task && result !== task.result_note);
  const tr = (key: string) => t(`taskboard.${key}`);
  const errorText = useCallback(
    (e: unknown) => {
      const code = (e as { code?: string })?.code;
      return t(
        `taskboard.errors.${code && ['VERSION_CONFLICT', 'REFERENCE_UNAVAILABLE', 'FEATURE_DISABLED', 'NOT_FOUND'].includes(code) ? code : 'generic'}`,
      );
    },
    [t],
  );
  const refresh = useCallback(async () => {
    const epoch = ++generation.current;
    setLoading(true);
    try {
      const rows = await Promise.all(statuses.map((s) => api.list(s, query, project)));
      if (epoch !== generation.current) return;
      setPages({ todo: rows[0], doing: rows[1], done: rows[2] });
    } catch (e) {
      if (epoch === generation.current) setError(errorText(e));
    } finally {
      if (epoch === generation.current) setLoading(false);
    }
  }, [query, project, errorText]);
  useEffect(() => {
    const timer = setTimeout(() => {
      void refresh();
    }, 200);
    return () => {
      clearTimeout(timer);
      generation.current++;
    };
  }, [refresh]);
  useEffect(() => {
    let active = true;
    void projectRegistryClient
      .list('all', 'code')
      .then((v) => {
        if (active) setProjects(v.projects.filter((p) => !p.is_default));
      })
      .catch((e) => {
        if (active) setError(errorText(e));
      });
    return () => {
      active = false;
    };
  }, [errorText]);
  useEffect(() => {
    let active = true;
    setTask(null);
    setResult('');
    if (taskId)
      void api
        .get(taskId)
        .then((v) => {
          if (active) {
            setTask(v.task);
            let draft: string | null = null;
            try {
              draft = sessionStorage.getItem(`taskboard:draft:${v.task.task_id}`);
            } catch {
              /* Private mode may disable storage. */
            }
            setResult(draft ?? v.task.result_note);
          }
        })
        .catch((e) => {
          if (active) setError(errorText(e));
        });
    return () => {
      active = false;
    };
  }, [taskId, errorText]);
  useEffect(() => {
    const focus = () => {
      void refresh();
    };
    window.addEventListener('focus', focus);
    return () => window.removeEventListener('focus', focus);
  }, [refresh]);
  useEffect(() => {
    if (!task) return;
    try {
      const key = `taskboard:draft:${task.task_id}`;
      if (result !== task.result_note) sessionStorage.setItem(key, result);
      else sessionStorage.removeItem(key);
    } catch {
      /* beforeunload still protects the current draft. */
    }
  }, [task, result]);
  useEffect(() => {
    const leave = (e: BeforeUnloadEvent) => {
      if (dirty) {
        e.preventDefault();
        e.returnValue = '';
      }
    };
    window.addEventListener('beforeunload', leave);
    return () => window.removeEventListener('beforeunload', leave);
  }, [dirty]);
  useEffect(() => {
    if (!picker) return;
    let active = true;
    setSessionLoading(true);
    setSessions([]);
    void projectRegistryClient
      .getSessions(sessionProject, sessionLimit)
      .then((v) => {
        if (active) {
          // project.get_sessions already projects owned Web sessions;
          // its public SessionInfo intentionally omits channel_id.
          setSessions(v.sessions.filter((s) => s.work_mode === 'code' && !s.cleanup_only));
          setSessionTotal(v.total);
        }
      })
      .catch((e) => {
        if (active) setError(errorText(e));
      })
      .finally(() => {
        if (active) setSessionLoading(false);
      });
    return () => {
      active = false;
    };
  }, [picker, sessionProject, sessionLimit, errorText]);
  const navigateTask = (id?: string) => {
    if (dirty && !window.confirm(tr('discard'))) return;
    if (task) {
      try {
        sessionStorage.removeItem(`taskboard:draft:${task.task_id}`);
      } catch {
        /* Optional draft cache. */
      }
    }
    setError('');
    onOpenTask?.(id);
  };
  const update = async (target: Task, patch: Record<string, unknown>) => {
    setBusy(true);
    setError('');
    try {
      const v = await api.update(target, patch);
      if (selectedRef.current === target.task_id) {
        setTask(v.task);
        if ('result_note' in patch) setResult(v.task.result_note);
      }
      setNotice(tr('saved'));
      await refresh();
      return true;
    } catch (e) {
      setError(errorText(e));
      return false;
    } finally {
      setBusy(false);
    }
  };
  const openForm = (edit = false) => {
    setEditing(edit);
    setForm(
      edit && task
        ? {
            title: task.title,
            description: task.description,
            priority: task.priority,
            project_id: task.project_id || '',
          }
        : { title: '', description: '', priority: 'normal', project_id: '' },
    );
    createKey.current = crypto.randomUUID();
  };
  const submit = async () => {
    if (!form || !form.title.trim()) return;
    setBusy(true);
    setError('');
    try {
      const values = {
        ...form,
        title: form.title.trim(),
        project_id: form.project_id || null,
      };
      if (editing && task) {
        const v = await api.update(task, values);
        setTask(v.task);
      } else {
        const v = await api.create(values, createKey.current);
        onOpenTask?.(v.task.task_id);
      }
      setForm(null);
      await refresh();
      setNotice(tr('saved'));
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  const loadMore = async (status: Status) => {
    const epoch = generation.current;
    setBusy(true);
    try {
      const v = await api.list(status, query, project, pages[status].next_cursor);
      if (epoch !== generation.current) return;
      setPages((p) => ({
        ...p,
        [status]: {
          tasks: [...p[status].tasks, ...v.tasks].filter(
            (x, i, a) => a.findIndex((y) => y.task_id === x.task_id) === i,
          ),
          next_cursor: v.next_cursor,
        },
      }));
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  const reloadTask = async () => {
    if (dirty && !window.confirm(tr('discard'))) return;
    setError('');
    await refresh();
    if (taskId) {
      try {
        const v = await api.get(taskId);
        setTask(v.task);
        setResult(v.task.result_note);
      } catch (e) {
        setError(errorText(e));
      }
    }
  };
  return (
    <section className="taskboard" data-testid="taskboard-root">
      <header className="taskboard-header">
        <div>
          <h1 data-testid="taskboard-title">{tr('title')}</h1>
          <p>{tr('subtitle')}</p>
        </div>
        <Button variant="primary" data-testid="taskboard-create" onClick={() => openForm()}>
          ＋ {tr('create')}
        </Button>
      </header>
      <div className="taskboard-toolbar">
        <Input
          changeOnBlur={false}
          value={query}
          onChange={setQuery}
          placeholder={tr('search')}
          aria-label={tr('search')}
          data-testid="taskboard-search"
        />
        <select
          value={project}
          onChange={(e) => setProject(e.target.value)}
          aria-label={tr('project')}
          data-testid="taskboard-project-filter"
        >
          <option value="">{tr('allProjects')}</option>
          {projects.map((p) => (
            <option key={p.project_id} value={p.project_id}>
              {p.name}
            </option>
          ))}
        </select>
        <Button
          onClick={() => {
            setError('');
            void refresh();
          }}
          data-testid="taskboard-refresh"
        >
          {tr('refresh')}
        </Button>
      </div>
      {error && (
        <div role="alert" className="taskboard-error" data-testid="taskboard-error">
          {error}{' '}
          <Button onClick={() => void reloadTask()} data-testid="taskboard-retry">
            {tr('reload')}
          </Button>
        </div>
      )}
      {notice && (
        <p role="status" className="taskboard-notice" data-testid="taskboard-notice">
          {notice}
        </p>
      )}
      <div className="taskboard-columns" aria-busy={loading}>
        {statuses.map((status) => (
          <section
            key={status}
            className={`taskboard-column taskboard-column--${status}`}
            data-testid="taskboard-column"
            data-variant={status}
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault();
              const id = e.dataTransfer.getData('text/plain');
              const target = statuses.flatMap((s) => pages[s].tasks).find((x) => x.task_id === id);
              if (target && !busy) void update(target, { status });
            }}
          >
            <h2>
              <span /> {tr(`status.${status}`)} <small>{pages[status].tasks.length}</small>
            </h2>
            {loading && !pages[status].tasks.length ? (
              <p>{tr('loading')}</p>
            ) : !pages[status].tasks.length ? (
              <p className="taskboard-empty" data-testid="taskboard-empty" data-variant={status}>
                {tr(query ? 'noMatches' : 'empty')}
              </p>
            ) : null}
            {pages[status].tasks.map((item) => (
              <article
                key={item.task_id}
                className="taskboard-card"
                draggable={!busy}
                onDragStart={(e) => e.dataTransfer.setData('text/plain', item.task_id)}
                data-testid="taskboard-card"
                data-variant={item.task_id}
              >
                <button onClick={() => navigateTask(item.task_id)} data-testid="taskboard-open-task">
                  <div className="taskboard-card-meta">
                    <span>TB-{String(item.number).padStart(3, '0')}</span>
                    <span className={`taskboard-priority taskboard-priority--${item.priority}`}>
                      {tr(`priority.${item.priority}`)}
                    </span>
                  </div>
                  <h3>{item.title}</h3>
                  <p>{item.description}</p>
                  <span className="taskboard-project">
                    {item.project?.available
                      ? item.project.title
                      : tr(item.project_id ? 'unavailableProject' : 'noProject')}
                  </span>
                  <footer>
                    <span>
                      {item.linked_session?.available
                        ? '↗ ' + item.linked_session.title
                        : tr(item.linked_session_id ? 'unavailableSession' : 'noSession')}
                    </span>
                    <time>{new Date(item.updated_at).toLocaleDateString(i18n.language)}</time>
                  </footer>
                </button>
              </article>
            ))}
            {pages[status].next_cursor && (
              <Button
                disabled={busy}
                onClick={() => void loadMore(status)}
                data-testid="taskboard-load-more"
                data-variant={status}
              >
                {tr('more')}
              </Button>
            )}
          </section>
        ))}
      </div>
      <p className="taskboard-hint">{tr('hint')}</p>
      <Dialog
        open={Boolean(taskId) && !form && !picker}
        titleId="taskboard-detail-heading"
        className="taskboard-detail"
        closeDisabled={busy}
        onCancel={() => navigateTask()}
      >
        <div data-testid="taskboard-detail">
          <header>
            <span>
              {tr('detail')} {task ? `TB-${String(task.number).padStart(3, '0')}` : ''}
            </span>
            <Button
              aria-label={tr('close')}
              onClick={() => navigateTask()}
              disabled={busy}
              data-testid="taskboard-close"
            >
              ×
            </Button>
          </header>
          {task ? (
            <>
              <div className="taskboard-detail-body">
                {error && (
                  <p role="alert" data-testid="taskboard-detail-error">
                    {error}{' '}
                    <Button onClick={() => void reloadTask()} data-testid="taskboard-detail-retry">
                      {tr('reload')}
                    </Button>
                  </p>
                )}
                <h2 id="taskboard-detail-heading">{task.title}</h2>
                <div className="taskboard-fields">
                  <label>
                    {tr('state')}
                    <select
                      value={task.status}
                      disabled={busy}
                      onChange={(e) => void update(task, { status: e.target.value })}
                      data-testid="taskboard-status"
                    >
                      {statuses.map((s) => (
                        <option key={s} value={s}>
                          {tr(`status.${s}`)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    {tr('priorityLabel')}
                    <select
                      value={task.priority}
                      disabled={busy}
                      onChange={(e) => void update(task, { priority: e.target.value })}
                      data-testid="taskboard-priority"
                    >
                      {priorities.map((p) => (
                        <option key={p} value={p}>
                          {tr(`priority.${p}`)}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
                <div className="taskboard-section-title">
                  <h3>{tr('description')}</h3>
                  <Button variant="quiet" disabled={busy} onClick={() => openForm(true)} data-testid="taskboard-edit">
                    {tr('edit')}
                  </Button>
                </div>
                <p className="taskboard-description">{task.description || tr('noDescription')}</p>
                <div className="taskboard-section-title">
                  <h3>{tr('session')}</h3>
                  <Button
                    variant="quiet"
                    disabled={busy}
                    onClick={() => {
                      setSessionProject(task.project_id || 'default_code');
                      setPicked(task.linked_session_id || '');
                      setPicker(true);
                    }}
                    data-testid="taskboard-link"
                  >
                    {tr(task.linked_session_id ? 'changeSession' : 'link')}
                  </Button>
                </div>
                <div className="taskboard-session">
                  <p>
                    {task.linked_session?.available
                      ? task.linked_session.title
                      : tr(task.linked_session_id ? 'unavailableSession' : 'noSession')}
                  </p>
                  {task.linked_session?.available && (
                    <Button
                      disabled={busy}
                      onClick={() => {
                        if (dirty && !window.confirm(tr('discard'))) return;
                        try {
                          sessionStorage.removeItem(`taskboard:draft:${task.task_id}`);
                        } catch {
                          /* Optional draft cache. */
                        }
                        void onOpenSession?.(task.linked_session_id!, task.task_id).catch((e) =>
                          setError(errorText(e)),
                        );
                      }}
                      data-testid="taskboard-open-session"
                    >
                      {tr('openSession')} ↗
                    </Button>
                  )}
                  {task.linked_session_id && (
                    <Button
                      variant="quiet"
                      disabled={busy}
                      onClick={() => void update(task, { linked_session_id: null })}
                      data-testid="taskboard-unlink"
                    >
                      {tr('unlink')}
                    </Button>
                  )}
                </div>
                <p className="taskboard-hint">{tr('sessionHint')}</p>
                <label className="taskboard-result-label">
                  {tr('result')}
                  <Textarea
                    value={result}
                    onChange={setResult}
                    rows={5}
                    maxLength={20000}
                    data-testid="taskboard-result"
                    placeholder={tr('resultPlaceholder')}
                  />
                </label>
                <p className="taskboard-hint">
                  {tr('updated')} · {new Date(task.updated_at).toLocaleString(i18n.language)}
                </p>
              </div>
              <footer>
                <Button
                  disabled={busy}
                  onClick={() => void update(task, { result_note: result })}
                  data-testid="taskboard-save-result"
                >
                  {tr('saveResult')}
                </Button>
                <Button
                  variant="primary"
                  disabled={busy}
                  onClick={() =>
                    void update(task, {
                      status: task.status === 'done' ? 'todo' : 'done',
                      result_note: result,
                    })
                  }
                  data-testid="taskboard-complete"
                >
                  {tr(task.status === 'done' ? 'reopen' : 'complete')}
                </Button>
              </footer>
            </>
          ) : (
            <p id="taskboard-detail-heading">{error || tr('loading')}</p>
          )}
        </div>
      </Dialog>
      <Dialog
        open={Boolean(form)}
        titleId="taskboard-form-heading"
        className="taskboard-modal"
        closeDisabled={busy}
        onCancel={() => setForm(null)}
      >
        <form
          data-testid="taskboard-form"
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          {form && (
            <>
              <h2 id="taskboard-form-heading">{tr(editing ? 'edit' : 'create')}</h2>
              <label>
                {tr('taskTitle')}
                <Input
                  required
                  maxLength={120}
                  changeOnBlur={false}
                  value={form.title}
                  onChange={(title) => setForm({ ...form, title })}
                  data-testid="taskboard-title-input"
                />
              </label>
              <label>
                {tr('description')}
                <Textarea
                  rows={4}
                  maxLength={20000}
                  value={form.description}
                  onChange={(description) => setForm({ ...form, description })}
                  data-testid="taskboard-description-input"
                />
              </label>
              <div className="taskboard-fields">
                <label>
                  {tr('project')}
                  <select
                    value={form.project_id}
                    onChange={(e) => setForm({ ...form, project_id: e.target.value })}
                    data-testid="taskboard-project-input"
                  >
                    <option value="">{tr('noProject')}</option>
                    {projects.map((p) => (
                      <option key={p.project_id} value={p.project_id}>
                        {p.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  {tr('priorityLabel')}
                  <select
                    value={form.priority}
                    onChange={(e) => setForm({ ...form, priority: e.target.value })}
                    data-testid="taskboard-priority-input"
                  >
                    {priorities.map((p) => (
                      <option key={p} value={p}>
                        {tr(`priority.${p}`)}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              {error && <p role="alert">{error}</p>}
              <footer>
                <Button disabled={busy} onClick={() => setForm(null)} data-testid="taskboard-cancel-form">
                  {tr('cancel')}
                </Button>
                <Button
                  type="submit"
                  variant="primary"
                  disabled={busy || !form.title.trim()}
                  data-testid="taskboard-submit"
                >
                  {tr(editing ? 'save' : 'create')}
                </Button>
              </footer>
            </>
          )}
        </form>
      </Dialog>
      <Dialog
        open={picker}
        titleId="taskboard-picker-heading"
        className="taskboard-modal"
        closeDisabled={busy}
        onCancel={() => setPicker(false)}
      >
        <div data-testid="taskboard-session-picker">
          <h2 id="taskboard-picker-heading">{tr('link')}</h2>
          <p>{tr('pickerHint')}</p>
          <label>
            {tr('project')}
            <select
              value={sessionProject}
              onChange={(e) => {
                setSessionProject(e.target.value);
                setSessionLimit(30);
                setPicked('');
              }}
              data-testid="taskboard-session-project"
            >
              <option value="default_code">{tr('noProject')}</option>
              {projects.map((p) => (
                <option key={p.project_id} value={p.project_id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          <div className="taskboard-session-list">
            {sessionLoading ? (
              <p>{tr('loading')}</p>
            ) : !sessions.length ? (
              <p>{tr('noSessions')}</p>
            ) : (
              sessions.map((s) => (
                <label
                  key={s.session_id}
                  className="taskboard-session-option"
                  data-testid="taskboard-session-option"
                  data-variant={s.session_id}
                >
                  <input
                    type="radio"
                    name="taskboard-session"
                    value={s.session_id}
                    checked={picked === s.session_id}
                    onChange={() => setPicked(s.session_id)}
                  />
                  <span>{s.display_title || s.title || s.session_id}</span>
                </label>
              ))
            )}
            {sessionTotal > sessionLimit && (
              <Button onClick={() => setSessionLimit((n) => n + 30)} data-testid="taskboard-more-sessions">
                {tr('more')}
              </Button>
            )}
          </div>
          {error && <p role="alert">{error}</p>}
          <footer>
            <Button disabled={busy} onClick={() => setPicker(false)} data-testid="taskboard-cancel-link">
              {tr('cancel')}
            </Button>
            <Button
              variant="primary"
              disabled={busy || sessionLoading || !sessions.some((s) => s.session_id === picked)}
              onClick={() => {
                if (task)
                  void update(task, { linked_session_id: picked }).then((ok) => {
                    if (ok) setPicker(false);
                  });
              }}
              data-testid="taskboard-confirm-link"
            >
              {tr('confirmLink')}
            </Button>
          </footer>
        </div>
      </Dialog>
    </section>
  );
}
