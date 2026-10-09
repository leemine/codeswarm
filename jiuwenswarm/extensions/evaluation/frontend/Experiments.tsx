import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useTranslation,
} from '../../../channels/web/frontend/src/applicationPlugins/ui';
import { Button, Input, Select } from '../../../channels/web/frontend/src/components/ui';
import { evaluationRequest as request } from './client';
import type { Catalog, Experiment, Options, TaskRef } from './types';

export default function Experiments({
  catalog,
  selected,
  onSelect,
  onLibrary,
}: {
  catalog: Catalog;
  onLibrary: () => void;
  selected: TaskRef[];
  onSelect: (tasks: TaskRef[]) => void;
}) {
  const { t } = useTranslation();
  const [step, setStep] = useState<number | null>(null);
  const [detailTab, setDetailTab] = useState('run');
  const selectedId = useRef(sessionStorage.getItem('evaluation:selected-experiment'));
  const [options, setOptions] = useState<Options>();
  const [model, setModel] = useState('');
  const [profile, setProfile] = useState('');
  const [name, setName] = useState('');
  const [timeout, setTimeout] = useState('300');
  const [repeats, setRepeats] = useState('1');
  const [policy, setPolicy] = useState('shared-environment-v1');
  const [acknowledged, setAcknowledged] = useState(false);
  const [experiments, setExperiments] = useState<Experiment[]>([]);
  const [detail, setDetail] = useState<Experiment>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const key = useRef(crypto.randomUUID());
  const mounted = useRef(true);
  const refreshRevision = useRef(0);
  const refresh = useCallback(async () => {
    const revision = ++refreshRevision.current;
    const { experiments: values } = await request<{
      experiments: Experiment[];
    }>('experiment.list');
    if (!mounted.current || revision !== refreshRevision.current) return;
    setExperiments(values);
    setDetail((current) => values.find((item) => item.id === (current?.id || selectedId.current)) || values[0]);
  }, []);
  const act = async (operation: () => Promise<void>) => {
    setBusy(true);
    setError('');
    try {
      await operation();
    } catch (err) {
      setError((err as { code?: string }).code || (err as Error).message);
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => {
    mounted.current = true;
    void request<Options>('options')
      .then((value) => {
        if (!mounted.current) return;
        setOptions(value);
        setModel(value.models[0]?.selection_key || '');
        setProfile(value.profiles[0]?.id || '');
      })
      .catch((err) => {
        if (mounted.current) setError(err.code || err.message);
      });
    let timer: ReturnType<typeof globalThis.setTimeout>;
    const poll = async () => {
      try {
        await refresh();
      } catch (err) {
        if (mounted.current) setError((err as Error).message);
      }
      if (mounted.current) timer = globalThis.setTimeout(() => void poll(), 2000);
    };
    void poll();
    return () => {
      mounted.current = false;
      refreshRevision.current += 1;
      globalThis.clearTimeout(timer);
    };
  }, [refresh]);
  useEffect(() => {
    if (detail) {
      selectedId.current = detail.id;
      sessionStorage.setItem('evaluation:selected-experiment', detail.id);
    }
  }, [detail?.id]);
  const create = async () => {
    const result = await request<Experiment>('experiment.create', {
      idempotency_key: key.current,
      experiment: {
        name,
        tasks: selected,
        model,
        execution_profile_id: profile,
        repeats: Number(repeats),
        timeout_seconds: Number(timeout),
        shared_environment_acknowledged: acknowledged,
        acceptance_policy: policy,
      },
    });
    // The create response confirms the freeze even if the following list read fails.
    refreshRevision.current += 1;
    setExperiments((current) => [result, ...current.filter((item) => item.id !== result.id)]);
    setDetail(result);
    setStep(null);
    setDetailTab('config');
    key.current = crypto.randomUUID();
    await refresh();
  };
  const exportEvidence = async (experiment: Experiment) => {
    // Fetch again through the authenticated host channel; cached UI is not authority.
    const fresh = await request<Experiment>('evidence', {
      experiment_id: experiment.id,
    });
    const url = URL.createObjectURL(new Blob([JSON.stringify(fresh, null, 2)], { type: 'application/json' }));
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `evaluation-${experiment.id}.json`;
    anchor.click();
    globalThis.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const firstAttempts = detail?.trials.map((trial) => trial.attempts[0]).filter(Boolean) || [];
  const validAcceptance = firstAttempts.filter(
    (attempt) => attempt.body.outcome === 'passed' || attempt.body.outcome === 'test_failed',
  ).length;
  const unfinished = firstAttempts.filter((attempt) => attempt.phase !== 'settled').length;
  const status = (value: string) => t(`evaluation.status.${value}`, { defaultValue: value });
  return (
    <section className="evaluation-panel evaluation-experiments" data-testid="evaluation-experiments">
      <div className="evaluation-actions">
        <Button
          variant="primary"
          data-testid="evaluation-new-experiment"
          onClick={() => {
            setStep(1);
            key.current = crypto.randomUUID();
            setAcknowledged(false);
          }}
        >
          {t('evaluation.newExperiment')}
        </Button>
        <Button data-testid="evaluation-run-refresh" disabled={busy} onClick={() => void act(refresh)}>
          {t('evaluation.refresh')}
        </Button>
      </div>
      {error && (
        <p role="alert" className="evaluation-error" data-testid="evaluation-experiment-error">
          {error}
        </p>
      )}
      {step !== null && (
        <section className="evaluation-wizard" data-testid="evaluation-wizard">
          <div className="evaluation-wizard-heading">
            <h2 data-testid="evaluation-wizard-title">{t('evaluation.newExperiment')}</h2>
            <Button data-testid="evaluation-wizard-close" disabled={busy} onClick={() => setStep(null)}>
              {t('evaluation.close')}
            </Button>
          </div>
          <ol className="evaluation-steps" data-testid="evaluation-steps">
            {['chooseTasks', 'configure', 'confirmFreeze'].map((label, index) => (
              <li
                key={label}
                aria-current={step === index + 1 ? 'step' : undefined}
                data-testid="evaluation-step"
                data-variant={index + 1}
              >
                {index + 1}. {t(`evaluation.${label}`)}
              </li>
            ))}
          </ol>
          {step === 1 && (
            <div data-testid="evaluation-wizard-tasks">
              <p data-testid="evaluation-task-choice-hint">{t('evaluation.taskChoiceHint')}</p>
              <div className="evaluation-actions">
                {catalog.datasets.map((dataset) => (
                  <Button
                    key={`${dataset.id}:${dataset.revision}`}
                    data-testid="evaluation-wizard-dataset"
                    data-variant={`${dataset.id}:${dataset.revision}`}
                    onClick={() => onSelect(dataset.value.tasks)}
                  >
                    {dataset.value.name} · v{dataset.revision}
                  </Button>
                ))}
                <Button data-testid="evaluation-wizard-library" onClick={onLibrary}>
                  {t('evaluation.library')}
                </Button>
              </div>
              <ul className="evaluation-list" data-testid="evaluation-wizard-task-list">
                {catalog.tasks.map((version) => (
                  <li
                    key={`${version.id}:${version.revision}`}
                    data-testid="evaluation-wizard-task"
                    data-variant={`${version.id}:${version.revision}`}
                  >
                    <label>
                      <input
                        type="checkbox"
                        data-testid="evaluation-wizard-task-select"
                        checked={selected.some(
                          (item) => item.task_id === version.id && item.revision === version.revision,
                        )}
                        onChange={() =>
                          onSelect(
                            selected.some((item) => item.task_id === version.id && item.revision === version.revision)
                              ? selected.filter(
                                  (item) => item.task_id !== version.id || item.revision !== version.revision,
                                )
                              : [
                                  ...selected,
                                  {
                                    task_id: version.id,
                                    revision: version.revision,
                                  },
                                ],
                          )
                        }
                      />
                      <span>
                        {version.value.name} · v{version.revision}
                      </span>
                    </label>
                    <span>
                      {t(
                        version.value.acceptance?.kind !== 'python'
                          ? 'evaluation.manualHint'
                          : 'evaluation.automaticAcceptance',
                      )}
                    </span>
                    <details data-testid="evaluation-task-preview">
                      <summary data-testid="evaluation-task-preview-toggle">{t('evaluation.view')}</summary>
                      <p data-testid="evaluation-task-preview-instruction">{version.value.instruction}</p>
                      <p data-testid="evaluation-task-preview-deliverables">
                        {t('evaluation.deliverables')}: {(version.value.deliverables || []).join(', ') || '—'}
                      </p>
                      <pre data-testid="evaluation-task-preview-acceptance">
                        {version.value.acceptance?.script || t('evaluation.manualHint')}
                      </pre>
                    </details>
                  </li>
                ))}
              </ul>
              {!catalog.tasks.length && <p data-testid="evaluation-no-tasks">{t('evaluation.noTasks')}</p>}
            </div>
          )}
          <div hidden={step !== 2} data-testid="evaluation-wizard-configuration">
            <h2 data-testid="evaluation-config-title">{t('evaluation.configure')}</h2>
            <p data-testid="evaluation-selected-tasks">
              {t('evaluation.selected', { count: selected.length })}:{' '}
              {selected.map((item) => `${item.task_id} v${item.revision}`).join(', ')}
            </p>
            <fieldset disabled={busy} className="evaluation-config" data-testid="evaluation-config-fields">
              <label>
                {t('evaluation.name')}
                <Input data-testid="evaluation-experiment-name" value={name} onChange={setName} />
              </label>
              <label>
                {t('evaluation.model')}
                <Select
                  data-testid="evaluation-model"
                  value={model}
                  options={
                    options?.models.map((item) => ({
                      value: item.selection_key,
                      label: item.display_name,
                    })) || []
                  }
                  onChange={setModel}
                />
              </label>
              <label>
                {t('evaluation.profile')}
                <Select
                  data-testid="evaluation-profile"
                  value={profile}
                  options={
                    options?.profiles.map((item) => ({
                      value: item.id,
                      label: `${item.id} · ${item.revision}`,
                    })) || []
                  }
                  onChange={setProfile}
                />
              </label>
              <label>
                {t('evaluation.repeats')}
                <Input
                  type="number"
                  min={1}
                  max={5}
                  data-testid="evaluation-repeats"
                  value={repeats}
                  onChange={setRepeats}
                />
              </label>
              <label>
                {t('evaluation.timeout')}
                <Input
                  type="number"
                  min={10}
                  max={1800}
                  data-testid="evaluation-timeout"
                  value={timeout}
                  onChange={setTimeout}
                />
              </label>
              <label>
                {t('evaluation.policy')}
                <Select
                  data-testid="evaluation-acceptance-policy"
                  value={policy}
                  options={(options?.acceptance_policies || ['shared-environment-v1']).map((value) => ({
                    value,
                    label: t(`evaluation.policies.${value}`),
                  }))}
                  onChange={setPolicy}
                />
              </label>
            </fieldset>
          </div>
          <div hidden={step !== 3} data-testid="evaluation-wizard-confirmation">
            <h3 data-testid="evaluation-confirm-name">{name}</h3>
            <p data-testid="evaluation-confirm-plan">
              {t('evaluation.confirmPlan', {
                tasks: selected.length,
                repeats,
                total: selected.length * Number(repeats),
              })}
            </p>
            <p data-testid="evaluation-confirm-model">
              Deepagent · {model} · {profile} · {timeout}s
            </p>
            <p data-testid="evaluation-confirm-policy">{t(`evaluation.policies.${policy}`)}</p>
            <p data-testid="evaluation-confirm-frozen">{t('evaluation.freezeOnCreate')}</p>
            <p data-testid="evaluation-policy-hint">
              {t(policy === 'independent-container-v1' ? 'evaluation.independentHint' : 'evaluation.shared')}
            </p>
            <label className="evaluation-ack">
              <input
                type="checkbox"
                data-testid="evaluation-acknowledge"
                checked={acknowledged}
                onChange={(event) => setAcknowledged(event.target.checked)}
              />
              <span>{t('evaluation.acknowledge')}</span>
            </label>
            <p data-testid="evaluation-cost-hint">{t('evaluation.costHint')}</p>
            {options && !options.execution_available && (
              <p data-testid="evaluation-execution-unavailable">{t('evaluation.localOnly')}</p>
            )}
            <Button
              variant="primary"
              data-testid="evaluation-create"
              disabled={
                busy ||
                !selected.length ||
                !name.trim() ||
                !model ||
                !profile ||
                !acknowledged ||
                !options?.execution_available
              }
              onClick={() => void act(create)}
            >
              {t('evaluation.create')}
            </Button>
          </div>
          <div className="evaluation-actions">
            {step > 1 && (
              <Button data-testid="evaluation-wizard-back" disabled={busy} onClick={() => setStep(step - 1)}>
                {t('evaluation.back')}
              </Button>
            )}
            {step < 3 && (
              <Button
                variant="primary"
                data-testid="evaluation-wizard-next"
                disabled={
                  busy ||
                  !selected.length ||
                  (step === 2 &&
                    (!name.trim() ||
                      !model ||
                      !profile ||
                      !options?.execution_available ||
                      !Number.isInteger(Number(repeats)) ||
                      Number(repeats) < 1 ||
                      Number(repeats) > 5 ||
                      Number(timeout) < 10 ||
                      Number(timeout) > 1800))
                }
                onClick={() => setStep(step + 1)}
              >
                {t('evaluation.next')}
              </Button>
            )}
          </div>
        </section>
      )}
      <h2 data-testid="evaluation-runs-title">{t('evaluation.runs')}</h2>
      <div className="evaluation-run-layout">
        <ul className="evaluation-list" data-testid="evaluation-run-list">
          {experiments.map((experiment) => (
            <li key={experiment.id} data-testid="evaluation-run" data-variant={experiment.id}>
              <Button
                data-testid="evaluation-run-open"
                aria-pressed={detail?.id === experiment.id}
                onClick={() => {
                  setDetail(experiment);
                  setDetailTab('run');
                }}
              >
                {experiment.definition.name}
              </Button>
              <span>
                {experiment.statistics?.passed || 0}/{experiment.statistics?.planned_trials || experiment.trials.length}
              </span>
            </li>
          ))}
        </ul>
        {!detail && <p data-testid="evaluation-no-runs">{t('evaluation.noRuns')}</p>}
        {detail && (
          <article data-testid="evaluation-run-detail">
            <h3 data-testid="evaluation-run-name">{detail.definition.name}</h3>
            <p data-testid="evaluation-frozen-hint">
              {t('evaluation.frozen')} · {detail.definition.model} ·{' '}
              {t(`evaluation.policies.${detail.definition.acceptance_policy}`)}
            </p>
            <div className="evaluation-actions">
              <Button
                variant="primary"
                data-testid="evaluation-start"
                disabled={
                  busy ||
                  detail.active ||
                  !detail.trials.some((trial) => trial.attempts.some((attempt) => attempt.phase === 'pending'))
                }
                onClick={() =>
                  void act(async () => {
                    setDetail(
                      await request('experiment.start', {
                        experiment_id: detail.id,
                      }),
                    );
                    await refresh();
                  })
                }
              >
                {t('evaluation.start')}
              </Button>
              <Button
                data-testid="evaluation-cancel"
                disabled={
                  busy || detail.trials.every((trial) => trial.attempts.every((attempt) => attempt.phase === 'settled'))
                }
                onClick={() =>
                  void act(async () => {
                    setDetail(
                      await request('experiment.cancel', {
                        experiment_id: detail.id,
                      }),
                    );
                    await refresh();
                  })
                }
              >
                {t('evaluation.cancel')}
              </Button>
              <Button
                data-testid="evaluation-export"
                disabled={busy}
                onClick={() => void act(() => exportEvidence(detail))}
              >
                {t('evaluation.export')}
              </Button>
              <Button
                data-testid="evaluation-copy"
                disabled={busy}
                onClick={() => {
                  onSelect(detail.definition.tasks);
                  setName(`${detail.definition.name} — ${t('evaluation.copy')}`);
                  setModel(detail.definition.model);
                  setProfile(detail.definition.execution_profile_id);
                  setTimeout(String(detail.definition.timeout_seconds));
                  setRepeats(String(detail.definition.repeats));
                  setPolicy(detail.definition.acceptance_policy);
                  key.current = crypto.randomUUID();
                  setAcknowledged(false);
                  setStep(1);
                }}
              >
                {t('evaluation.copy')}
              </Button>
            </div>
            <div className="evaluation-actions" role="tablist" data-testid="evaluation-detail-tabs">
              {['run', 'results', 'config'].map((tab) => (
                <Button
                  key={tab}
                  role="tab"
                  aria-selected={detailTab === tab}
                  data-testid="evaluation-detail-tab"
                  data-variant={tab}
                  onClick={() => setDetailTab(tab)}
                >
                  {t(`evaluation.detailTabs.${tab}`)}
                </Button>
              ))}
            </div>
            <div hidden={detailTab === 'config'} data-testid="evaluation-detail-outcomes">
              <p data-testid="evaluation-summary">
                {t(detail.statistics?.all_settled ? 'evaluation.finalRatio' : 'evaluation.partialRatio', {
                  passed: detail.statistics?.passed || 0,
                  total: detail.trials.length,
                })}
              </p>
              <p data-testid="evaluation-valid-samples">
                {t('evaluation.validSamples', {
                  passed: detail.statistics?.passed || 0,
                  valid: validAcceptance,
                  excluded: detail.trials.length - validAcceptance,
                  unfinished,
                })}
              </p>
              <p data-testid="evaluation-usage">{t('evaluation.unknownUsage')}</p>
              <div className="evaluation-actions" data-testid="evaluation-outcome-counts">
                {Object.entries(detail.statistics?.first_attempt_outcomes || {}).map(([outcome, count]) => (
                  <span key={outcome} data-testid="evaluation-outcome-count" data-variant={outcome}>
                    {outcome === 'passed' && detail.definition.acceptance_policy === 'independent-container-v1'
                      ? t('evaluation.independentPassed')
                      : status(outcome)}: {count}
                  </span>
                ))}
              </div>
              <ul className="evaluation-trials" data-testid="evaluation-trials">
                {detail.trials.map((trial) => (
                  <li key={trial.id} data-testid="evaluation-trial" data-variant={trial.id}>
                    <h3>
                      {trial.task_id} ·{' '}
                      {t('evaluation.repeatIndex', {
                        index: trial.repeat_index + 1,
                      })}
                    </h3>
                    {trial.attempts.map((attempt) => (
                      <div key={attempt.id} data-testid="evaluation-attempt" data-variant={attempt.id}>
                        <p data-testid="evaluation-attempt-status">
                          {t('evaluation.executionState')}:{' '}
                          {status(
                            attempt.body.status === 'stopping'
                              ? 'stopping'
                              : attempt.body.status === 'recovery_required'
                                ? 'recovery_required'
                                : attempt.body.runtime?.state || attempt.body.status || attempt.phase,
                          )}{' '}
                        </p>
                        <p data-testid="evaluation-attempt-outcome">
                          {t('evaluation.acceptanceResult')}:{' '}
                          {attempt.body.outcome === 'passed' &&
                          detail.definition.acceptance_policy === 'independent-container-v1'
                            ? t('evaluation.independentPassed')
                            : status(attempt.body.outcome || 'not_evaluated')}
                        </p>
                        {attempt.body.session_id && (
                          <a
                            data-testid="evaluation-session-link"
                            href={`/chat/${encodeURIComponent(attempt.body.session_id)}`}
                            target="_blank"
                            rel="noreferrer"
                          >
                            {t('evaluation.session')}
                          </a>
                        )}
                        {attempt.body.error_code && (
                          <p data-testid="evaluation-attempt-error">{attempt.body.error_code}</p>
                        )}
                        <details
                          hidden={detailTab !== 'results'}
                          data-testid="evaluation-evidence"
                          open={detailTab === 'results'}
                        >
                          <summary data-testid="evaluation-evidence-toggle">{t('evaluation.evidence')}</summary>
                          <p data-testid="evaluation-exit">
                            {t('evaluation.exitConfirmed')}:{' '}
                            {t(attempt.body.exit_confirmed ? 'evaluation.yes' : 'evaluation.no')}
                          </p>
                          <p data-testid="evaluation-workspace">{attempt.body.workspace}</p>
                          {attempt.body.verification_environment && (
                            <div data-testid="evaluation-independent-evidence">
                              <p data-testid="evaluation-verifier-image">
                                {t('evaluation.verifierImage')}: {attempt.body.verification_environment.image_id}
                              </p>
                              <p data-testid="evaluation-authority-digest">
                                {t('evaluation.authorityDigest')}: {attempt.body.authority_sha256}
                              </p>
                              <p data-testid="evaluation-assertion-count">
                                {t('evaluation.assertions')}: {attempt.body.authoritative_assertions ?? '—'}
                              </p>
                              <p data-testid="evaluation-verifier-cleanup">
                                {t('evaluation.verifierCleanup')}:{' '}
                                {t(attempt.body.verifier_removed ? 'evaluation.yes' : 'evaluation.no')}
                              </p>
                            </div>
                          )}
                          {attempt.body.test_output !== undefined && (
                            <pre data-testid="evaluation-test-output">
                              {attempt.body.test_output || t('evaluation.emptyOutput')}
                            </pre>
                          )}
                          {attempt.body.files?.map((file) => (
                            <div key={file.path} data-testid="evaluation-delivery" data-variant={file.path}>
                              <p>
                                {file.path} · {file.status} · {file.sha256}
                              </p>
                              <pre data-testid="evaluation-diff">{file.diff}</pre>
                            </div>
                          ))}
                          <pre data-testid="evaluation-attempt-json">{JSON.stringify(attempt.body, null, 2)}</pre>
                        </details>
                      </div>
                    ))}
                  </li>
                ))}
              </ul>
            </div>
            <details hidden={detailTab !== 'config'} open={detailTab === 'config'} data-testid="evaluation-snapshot">
              <summary data-testid="evaluation-snapshot-toggle">{t('evaluation.snapshot')}</summary>
              <pre data-testid="evaluation-snapshot-json">
                {JSON.stringify({ definition: detail.definition, versions: detail.versions }, null, 2)}
              </pre>
            </details>
          </article>
        )}
      </div>
    </section>
  );
}
