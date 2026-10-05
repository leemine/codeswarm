import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Select } from '../../components/ui/Select/Select';
import { Input } from '../../components/ui/Input/Input';
import { projectRegistryClient } from '../../features/workspace/projectRegistryClient';
import type { ProjectInfo } from '../../features/workspace/projectTypes';
import { onOrganizationCredentialChange } from '../../services/organizationCredentialEvents';
import { webClient } from '../../services/webClient';
import {
  sessionSharingApi,
  type SessionShare,
  type ContinuationOption,
  type ContinuationInput,
  type ContinuedSession,
} from '../../services/sessionSharingApi';
import {
  isContinuationOutcomeUnknown,
  newContinuationAttempt,
  type ContinuationAttempts,
} from '../state/continueSharedSession';

export type OnContinued = (
  result: ContinuedSession,
  input: Readonly<ContinuationInput>,
  isCurrent: () => boolean,
) => Promise<void>;

/** One received grant; changing grants remounts the pane and invalidates replies. */
export function ContinuationPane({
  share,
  onContinued,
  onCancel,
  attempts,
}: {
  share: SessionShare;
  onContinued: OnContinued;
  onCancel: () => void;
  attempts: ContinuationAttempts;
}) {
  const { t } = useTranslation();
  const attemptKey = JSON.stringify([share.session_id, share.share_id, share.revision]);
  const savedAttempt = attempts.get(attemptKey);
  const [projects, setProjects] = useState<ProjectInfo[]>([]);
  const [project, setProject] = useState('');
  const [options, setOptions] = useState<ContinuationOption[]>([]);
  const [optionKey, setOptionKey] = useState('');
  const [title, setTitle] = useState('');
  const [loading, setLoading] = useState(false);
  const [pending, setPending] = useState(false);
  const [connected, setConnected] = useState(webClient.getState() === 'ready');
  const [error, setError] = useState<'failed' | 'unknown' | null>(savedAttempt?.outcome ?? null);
  const [attempt, setAttempt] = useState<Readonly<ContinuationInput> | null>(savedAttempt?.input ?? null);
  const attemptRef = useRef<Readonly<ContinuationInput> | null>(savedAttempt?.input ?? null);
  const generation = useRef(0);
  const busy = useRef(false);
  const keyFor = (option: ContinuationOption) =>
    JSON.stringify([option.execution_profile_id, option.mode, option.model_name]);

  function invalidate(auth = false) {
    generation.current += 1;
    busy.current = false;
    setPending(false);
    setLoading(false);
    setProjects([]);
    setOptions([]);
    setOptionKey('');
    setProject('');
    setTitle('');
    setError(attemptRef.current && !auth ? 'unknown' : 'failed');
    if (auth) {
      attempts.clear();
      attemptRef.current = null;
      setAttempt(null);
    }
  }

  async function loadProjects() {
    const current = ++generation.current;
    setLoading(true);
    setError(null);
    setProjects([]);
    setOptions([]);
    setOptionKey('');
    try {
      const response = await projectRegistryClient.list();
      if (current !== generation.current) return;
      setProjects(response.projects);
    } catch {
      if (current === generation.current) setError('failed');
    } finally {
      if (current === generation.current) setLoading(false);
    }
  }

  useEffect(() => {
    if (!attemptRef.current) void loadProjects();
    const offAuth = onOrganizationCredentialChange(() => invalidate(true));
    const offConnection = webClient.onStateChange((state) => {
      setConnected(state === 'ready');
      if (state !== 'ready') invalidate();
    });
    return () => {
      generation.current += 1;
      offAuth();
      offConnection();
    };
  }, []);

  async function selectProject(id: string) {
    const current = ++generation.current;
    setProject(id);
    setOptions([]);
    setOptionKey('');
    setError(null);
    if (!id) {
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const rows = await sessionSharingApi.continuationOptions({
        session_id: share.session_id,
        share_id: share.share_id,
        expected_revision: share.revision,
        target_project_id: id,
      });
      if (current !== generation.current) return;
      setOptions(rows);
      // Selection comes from a verified complete tuple, never a default profile.
      if (rows.length === 1) setOptionKey(keyFor(rows[0]));
    } catch {
      if (current === generation.current) setError('failed');
    } finally {
      if (current === generation.current) setLoading(false);
    }
  }

  async function submit() {
    if (busy.current || !connected) return;
    const selected = options.find((row) => keyFor(row) === optionKey);
    let input = attemptRef.current;
    if (!input) {
      if (!selected || !project) return;
      input = newContinuationAttempt({
        session_id: share.session_id,
        share_id: share.share_id,
        expected_revision: share.revision,
        target_project_id: project,
        execution_profile_id: selected.execution_profile_id,
        mode: selected.mode,
        model_name: selected.model_name,
        title: title.trim(),
      });
      attemptRef.current = input;
      attempts.set(attemptKey, { input, outcome: 'unknown' });
      setAttempt(input);
    }
    attempts.set(attemptKey, { input, outcome: 'unknown' });
    busy.current = true;
    const current = ++generation.current;
    const isCurrent = () => current === generation.current && webClient.getState() === 'ready';
    setPending(true);
    setError(null);
    try {
      const result = await sessionSharingApi.continueSession(input);
      if (!isCurrent()) return;
      await onContinued(result, input, isCurrent);
      if (isCurrent()) attempts.delete(attemptKey);
    } catch (failure) {
      if (current === generation.current) {
        const outcome = isContinuationOutcomeUnknown(failure) ? 'unknown' : 'failed';
        attempts.set(attemptKey, { input, outcome });
        setError(outcome);
      }
    } finally {
      if (current === generation.current) {
        busy.current = false;
        setPending(false);
      }
    }
  }

  function newInput() {
    if (busy.current) return;
    attempts.delete(attemptKey);
    attemptRef.current = null;
    setAttempt(null);
    setProject('');
    setTitle('');
    void loadProjects();
  }

  return (
    <section className="session-continuation-pane" data-testid="multi-session-continuation-pane">
      <h3 data-testid="multi-session-continuation-title">{t('sessionSharing.continuation.title')}</h3>
      <p data-testid="multi-session-continuation-scope">{t('sessionSharing.continuation.scope')}</p>
      {attempt && (
        <p data-testid="multi-session-continuation-saved-request">
          {t('sessionSharing.continuation.savedRequest', {
            project: attempt.target_project_id,
            profile: attempt.execution_profile_id,
            model: attempt.model_name,
            title: attempt.title,
          })}
        </p>
      )}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
        data-testid="multi-session-continuation-form"
      >
        <label data-testid="multi-session-continuation-project-label">
          {t('sessionSharing.continuation.project')}
          <Select
            value={project}
            options={[
              { value: '', label: t('sessionSharing.continuation.selectProject') },
              ...projects.map((item) => ({ value: item.project_id, label: item.name })),
            ]}
            onChange={(value) => void selectProject(value)}
            disabled={pending || Boolean(attempt) || !connected}
            data-testid="multi-session-continuation-project"
          />
        </label>
        <label data-testid="multi-session-continuation-option-label">
          {t('sessionSharing.continuation.option')}
          <Select
            value={optionKey}
            options={[
              { value: '', label: t('sessionSharing.continuation.selectOption') },
              ...options.map((option) => ({ value: keyFor(option), label: option.label })),
            ]}
            onChange={setOptionKey}
            disabled={loading || pending || Boolean(attempt) || !connected}
            data-testid="multi-session-continuation-option"
          />
        </label>
        <label data-testid="multi-session-continuation-name-label">
          {t('sessionSharing.continuation.name')}
          <Input
            value={title}
            onChange={setTitle}
            maxLength={100}
            disabled={pending || Boolean(attempt) || !connected}
            data-testid="multi-session-continuation-name"
          />
        </label>
        {loading && (
          <p role="status" data-testid="multi-session-continuation-loading">
            {t('sessionSharing.loading')}
          </p>
        )}
        {!loading && project && !options.length && !error && (
          <p data-testid="multi-session-continuation-empty">{t('sessionSharing.continuation.empty')}</p>
        )}
        {error && (
          <p
            role="alert"
            className="session-sharing-error"
            data-testid="multi-session-continuation-error"
            data-variant={error}
          >
            {t(`sessionSharing.continuation.${error}`)}
          </p>
        )}
        <div className="session-sharing-row-actions">
          <button
            type="submit"
            disabled={
              loading ||
              pending ||
              !connected ||
              (attempt ? error !== 'unknown' : !options.some((row) => keyFor(row) === optionKey))
            }
            data-testid="multi-session-continuation-submit"
            data-variant={attempt ? 'retry' : 'create'}
          >
            {t(attempt ? 'sessionSharing.continuation.retry' : 'sessionSharing.continuation.create')}
          </button>
          <button
            type="button"
            onClick={newInput}
            disabled={pending || !connected}
            data-testid="multi-session-continuation-new-input"
          >
            {t('sessionSharing.continuation.newInput')}
          </button>
          <button
            type="button"
            onClick={() => {
              generation.current += 1;
              onCancel();
            }}
            data-testid="multi-session-continuation-cancel"
          >
            {t('common.cancel')}
          </button>
        </div>
      </form>
    </section>
  );
}
